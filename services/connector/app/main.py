"""connector — the only restor8 service that touches devices.

Every other service (backup, restore, scenario) reaches lab devices
through THIS service's REST API, never by importing PyEZ itself (spec
§2). One choke-point means one owner for connection lifecycle, retry,
and the progress-event stream that the gateway (Phase 6) fans out to
the browser.

Phase 0 surface: ``POST /connect`` proves the wire works — NETCONF open,
real facts, full event sequence in the logs. Push/backup endpoints land
with their phases.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from restor8_core.events import DeviceEvent
from restor8_core.junos import ConfigFormat, ConfigMode, JunosConnection
from restor8_core.models import Restor8Error

log = logging.getLogger("restor8.connector")
events_log = logging.getLogger("restor8.events")

# Make progress events visible under stock uvicorn (its default config only
# configures its own loggers — without this, INFO events have no handler
# and never reach stdout). One JSON object per line, ready for Loki.
logging.basicConfig(level=logging.INFO, format="%(message)s")

# The NETCONF/SSH transports chat at INFO (every RPC exchange, paramiko
# banners) and drown the event stream in pod logs. restor8.events is the
# signal; device transcripts are not wanted on stdout.
for _noisy in ("ncclient", "paramiko"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

app = FastAPI(
    title="restor8 connector",
    description="Owns all live PyEZ/NETCONF sessions for the restor8 platform.",
    version="0.1.0",
)


class ConnectRequest(BaseModel):
    """One-shot connectivity probe payload.

    Credential resolution order (first hit wins):

    1. explicit ``user``/``auth`` in the request,
    2. ``auth_ref`` → env ``LAB_AUTH_<REF>_USER``/``LAB_AUTH_<REF>_PASSWORD``
       (ref upper-cased, dashes→underscores; each k8s Secret the cluster
       knows is injected as such a pair — see the Deployment),
    3. the default lab credential env ``LAB_USER``/``LAB_PASSWORD``
       (Secret ``restor8/lab-auth``).
    """

    host: str = Field(description="device mgmt IP/hostname")
    port: int = Field(default=830, description="NETCONF port (Junos default 830)")
    user: str | None = Field(default=None, description="SSH user; overrides auth_ref/env")
    auth: str | None = Field(default=None, description="SSH password; overrides auth_ref/env")
    auth_ref: str | None = Field(
        default=None,
        description="inventory auth_ref (k8s Secret name), e.g. "
        "'lab-auth-root' → env LAB_AUTH_ROOT_USER/LAB_AUTH_ROOT_PASSWORD "
        "(upper-cased, dashes→underscores, _USER/_PASSWORD appended)",
    )
    timeout: int = Field(default=30, description="per-RPC timeout (seconds)")


def _resolve_creds(req: ConnectRequest) -> tuple[str | None, str | None]:
    """Resolve (user, password) per the order documented on ConnectRequest.

    Args:
        req: any request carrying the credential fields.

    Returns:
        The (user, password) pair; either may be None (caller 422s).
    """
    if req.user and req.auth:
        return req.user, req.auth
    if req.auth_ref:
        # auth_ref is a Secret name verbatim: "lab-auth-root" →
        # LAB_AUTH_ROOT_USER / LAB_AUTH_ROOT_PASSWORD. Checked BEFORE the
        # default pair — in-cluster the defaults are always set, so an
        # auth_ref that lost to them would silently authenticate with the
        # wrong credential.
        prefix = req.auth_ref.upper().replace("-", "_")
        user = req.user or os.environ.get(f"{prefix}_USER")
        auth = req.auth or os.environ.get(f"{prefix}_PASSWORD")
        if user and auth:
            return user, auth
    return req.user or os.environ.get("LAB_USER"), req.auth or os.environ.get("LAB_PASSWORD")


class ConnectResponse(BaseModel):
    """Facts + the event-sequence echo of the probe."""

    session_id: str = Field(description="correlates with the event stream")
    facts: dict[str, object] = Field(description="PyEZ facts as reported by the device")


def _log_event(event: DeviceEvent) -> None:
    """Emit one progress event as a single JSON log line.

    Structured from day one (spec Phase 8): Loki/Grafana can index the
    stage/device fields without regexing prose, and the gateway will
    later ship the same objects over WebSocket.
    """
    events_log.info(json.dumps(event.model_dump(mode="json"), default=str))


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — also the target of the smoke-test 200."""
    return {"service": "restor8-connector", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target (manifests point here)."""
    return {"status": "ok"}


@app.post("/connect", response_model=ConnectResponse)
def connect(req: ConnectRequest) -> ConnectResponse:
    """Open a NETCONF session, return real facts, close.

    Sync endpoint on purpose: FastAPI runs it in a threadpool, matching
    PyEZ's blocking nature. Every stage (resolving → connecting →
    authenticating → connected → closed) is logged as JSON on the way.

    Args:
        req: host + optional credential overrides (falls back to the
            shared lab credential env vars).

    Returns:
        The device facts and the session's correlation ID.

    Raises:
        HTTPException 422: no credentials in payload or environment.
        HTTPException 502: device unreachable / auth failed / RPC timeout
            (typed restor8 errors, original Junos message intact).
    """
    user, auth = _resolve_creds(req)
    if not user or not auth:
        raise HTTPException(
            status_code=422,
            detail=(
                "no credentials: pass user/auth or auth_ref, or inject the "
                "lab credential via env (k8s Secrets restor8/lab-auth*)"
            ),
        )

    jc = JunosConnection(
        req.host,
        user,
        auth,
        port=req.port,
        timeout=req.timeout,
        on_event=_log_event,
    )
    try:
        facts = jc.connect()
    except Restor8Error as exc:
        raise _device_error(exc) from exc
    except Exception as exc:
        # Unmapped failures must surface as structured errors too — a bare
        # 500 is undiscoverable from the UI. The traceback goes to the
        # service log for diagnosis.
        raise _unmapped_error(req.host, exc) from exc
    finally:
        jc.close()

    return ConnectResponse(
        session_id=jc.session_id, facts=facts.model_dump(mode="json")
    )


class ConfigRequest(ConnectRequest):
    """Pull the running configuration (backup service's device read)."""

    fmt: ConfigFormat = Field(
        default="text",
        description="config format — MUST stay stable per device or Git "
        "diffs become meaningless",
    )


class ConfigResponse(BaseModel):
    """The device's running config as a string."""

    session_id: str
    config: str


@app.post("/config", response_model=ConfigResponse)
def get_config(req: ConfigRequest) -> ConfigResponse:
    """Open a session, fetch the running config, close.

    Full event sequence (resolving → … → connected → closed) is logged
    like /connect — backup can stream it per device.

    Raises:
        HTTPException 422: no resolvable credentials.
        HTTPException 502: typed device error (unreachable/auth/rpc).
    """
    user, auth = _resolve_creds(req)
    if not user or not auth:
        raise HTTPException(status_code=422, detail="no credentials (see /connect)")
    jc = JunosConnection(
        req.host, user, auth, port=req.port, timeout=req.timeout,
        on_event=_log_event,
    )
    try:
        jc.connect()
        config = jc.get_config(req.fmt)
    except Restor8Error as exc:
        raise _device_error(exc) from exc
    except Exception as exc:
        raise _unmapped_error(req.host, exc) from exc
    finally:
        jc.close()
    return ConfigResponse(session_id=jc.session_id, config=config)


class PushRequest(ConfigRequest):
    """Push config through the confirmed-commit pipeline.

    ``confirm_now=True`` (default) finalises the commit immediately —
    right for idempotent pushes. ``confirm_now=False`` leaves the
    confirmed-commit window open for validation (JSNAPy post-check);
    a confirming commit must come from the SAME NETCONF session, so
    finalising those is deferred to connector's session-holding API,
    which lands with restore (Phase 3) — until then, the window simply
    expires and the device self-reverts.
    """

    payload: str = Field(description="config text in `fmt` form")
    mode: ConfigMode = Field(
        default="merge",
        description="merge (additive) | override (whole-config replace)",
    )
    confirm_minutes: int = Field(default=2, description="confirmed-commit window")
    comment: str = Field(default="restor8", description="Junos commit comment")
    confirm_now: bool = Field(default=True, description="finalise immediately")


class PushResponse(BaseModel):
    """The pending diff and whether the commit was finalised."""

    session_id: str
    diff: str
    confirmed: bool


@app.post("/push", response_model=PushResponse)
def push_config(req: PushRequest) -> PushResponse:
    """lock → load → diff → commit confirmed (→ confirm) → unlock → close.

    Raises:
        HTTPException 422: no resolvable credentials.
        HTTPException 502: typed device error; the Junos error text
            (syntax error, lock held, …) rides in ``detail.message``.
    """
    user, auth = _resolve_creds(req)
    if not user or not auth:
        raise HTTPException(status_code=422, detail="no credentials (see /connect)")
    jc = JunosConnection(
        req.host, user, auth, port=req.port, timeout=req.timeout,
        on_event=_log_event,
    )
    try:
        jc.connect()
        diff = jc.push_config(
            req.payload,
            fmt=req.fmt,
            mode=req.mode,
            confirm_minutes=req.confirm_minutes,
            comment=req.comment,
        )
        confirmed = False
        if req.confirm_now:
            jc.confirm_commit()
            confirmed = True
    except Restor8Error as exc:
        raise _device_error(exc) from exc
    except Exception as exc:
        raise _unmapped_error(req.host, exc) from exc
    finally:
        jc.close()
    return PushResponse(session_id=jc.session_id, diff=diff, confirmed=confirmed)


def _device_error(exc: Restor8Error) -> HTTPException:
    """Build the structured 502 for a typed device error."""
    return HTTPException(
        status_code=502,
        detail={
            "error": exc.__class__.__name__,
            "device": exc.device,
            "stage": exc.stage,
            "message": str(exc),
        },
    )


def _unmapped_error(host: str, exc: Exception) -> HTTPException:
    """Build the structured 502 for an unmapped failure; log traceback."""
    log.exception("unmapped failure on %s", host)
    return HTTPException(
        status_code=502,
        detail={
            "error": exc.__class__.__name__,
            "device": host,
            "stage": "",
            "message": str(exc),
        },
    )
