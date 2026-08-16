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
from restor8_core.junos import JunosConnection
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

    ``user``/``auth`` may be omitted in-cluster: the deployment injects
    the shared lab credential (k8s Secret ``restor8/lab-auth``) as
    ``LAB_USER``/``LAB_PASSWORD`` env vars. Explicit values win.
    """

    host: str = Field(description="device mgmt IP/hostname")
    port: int = Field(default=830, description="NETCONF port (Junos default 830)")
    user: str | None = Field(default=None, description="SSH user; default LAB_USER")
    auth: str | None = Field(
        default=None, description="SSH password; default LAB_PASSWORD"
    )
    timeout: int = Field(default=30, description="per-RPC timeout (seconds)")


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
    user = req.user or os.environ.get("LAB_USER")
    auth = req.auth or os.environ.get("LAB_PASSWORD")
    if not user or not auth:
        raise HTTPException(
            status_code=422,
            detail=(
                "no credentials: pass user/auth, or inject the shared lab "
                "credential via env LAB_USER/LAB_PASSWORD (k8s Secret "
                "restor8/lab-auth)"
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
        raise HTTPException(
            status_code=502,
            detail={
                "error": exc.__class__.__name__,
                "device": exc.device,
                "stage": exc.stage,
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        # Unmapped failures must surface as structured errors too — a bare
        # 500 is undiscoverable from the UI. The traceback goes to the
        # service log for diagnosis.
        log.exception("unmapped failure connecting to %s", req.host)
        raise HTTPException(
            status_code=502,
            detail={
                "error": exc.__class__.__name__,
                "device": req.host,
                "stage": "",
                "message": str(exc),
            },
        ) from exc
    finally:
        jc.close()

    return ConnectResponse(
        session_id=jc.session_id, facts=facts.model_dump(mode="json")
    )
