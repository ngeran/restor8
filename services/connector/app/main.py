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

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from restor8_core.events import DeviceEvent, relay_sink
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


# ── credential profiles (live k8s Secrets) ─────────────────────────────
#
# auth_ref Secrets are read from the k8s API AT REQUEST TIME: a new or
# rotated profile (POST /credentials) takes effect immediately — no pod
# restart, no manifest edit. The statically-injected env pairs remain the
# first-line fallback for bootstrap credentials; the API is the day-2 path.
# Passwords are WRITE-ONLY here: listing never returns them.

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PROFILE_PREFIX = "lab-auth"


def _k8s_headers() -> dict[str, str] | None:
    """Auth headers for the in-cluster API, or None outside a cluster."""
    token_file = _SA / "token"
    if not token_file.exists():
        return None
    return {"Authorization": f"Bearer {token_file.read_text().strip()}"}


def _k8s_secret(name: str) -> dict[str, str] | None:
    """Read one Secret's LAB_USER/LAB_PASSWORD (None if absent/off-cluster).

    Raises:
        Restor8Error-ish RuntimeError: the API answered but not 200/404
        (RBAC misconfig etc.) — surfaced so misconfiguration isn't silent.
    """
    headers = _k8s_headers()
    if headers is None:
        return None
    ns = (_SA / "namespace").read_text().strip() if (_SA / "namespace").exists() else "restor8"
    ca = str(_SA / "ca.crt") if (_SA / "ca.crt").exists() else False
    r = httpx.get(
        f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/secrets/{name}",
        headers=headers,
        verify=ca,
        timeout=10,
    )
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"k8s API {r.status_code} reading secret {name}: {r.text[:150]}")
    data = r.json().get("data", {})
    try:
        return {
            "user": base64.b64decode(data["LAB_USER"]).decode(),
            "password": base64.b64decode(data["LAB_PASSWORD"]).decode(),
        }
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"secret {name} lacks LAB_USER/LAB_PASSWORD keys") from exc


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
        # 1) the LIVE profile Secret FIRST — POST /credentials rotation must
        #    take effect immediately, and the pod's injected env is a stale
        #    snapshot from ITS startup (rotation-through-env silently kept
        #    using the old password until restart — seen live, never again).
        live = _k8s_secret(req.auth_ref)
        if live:
            return req.user or live["user"], req.auth or live["password"]
        # 2) the statically-injected env pair (bootstrap / API-unreachable
        #    fallback): "lab-auth-root" → LAB_AUTH_ROOT_USER / _PASSWORD
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


# ── held sessions ──────────────────────────────────────────────────────
#
# A confirming commit must come from the SAME NETCONF session that issued
# `commit confirmed`, so the validate-then-confirm flow (restore/scenario)
# needs connector to hold a session open across requests: /push with
# confirm_now=false parks it here for the confirmed-commit window; /session
# then confirms or rolls it back. If nobody decides in time, the DEVICE
# auto-reverts on its own (that's the whole point of confirmed commits) —
# the sweeper below only reaps our side so nothing leaks.


class SessionRegistry:
    """Thread-safe registry of held NETCONF sessions with expiry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[JunosConnection, float]] = {}

    def hold(self, jc: JunosConnection, ttl_seconds: float) -> None:
        """Park a session until its confirmed-commit window closes."""
        with self._lock:
            self._sessions[jc.session_id] = (jc, time.monotonic() + ttl_seconds)

    def take(self, session_id: str) -> JunosConnection | None:
        """Pop a live session (None if unknown or expired)."""
        with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            return None
        jc, expires_at = entry
        if time.monotonic() > expires_at:
            jc.close()
            return None
        return jc

    def peek(self, session_id: str) -> dict[str, object] | None:
        """Status of a held session without taking it."""
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            return None
        jc, expires_at = entry
        return {
            "session_id": session_id,
            "host": jc.host,
            "expires_in": round(max(0.0, expires_at - time.monotonic()), 1),
        }

    def sweep(self) -> None:
        """Close and drop expired sessions (device already self-reverted)."""
        now = time.monotonic()
        with self._lock:
            expired = [
                (sid, jc)
                for sid, (jc, at) in self._sessions.items()
                if at < now
            ]
            for sid, _ in expired:
                del self._sessions[sid]
        # close outside the lock — close() does device I/O
        for _, jc in expired:
            jc.close()


def _sweep_forever() -> None:
    """Reap held sessions every 15s for the process lifetime."""
    while True:
        time.sleep(15)
        sessions.sweep()


sessions = SessionRegistry()


@app.on_event("startup")
def _start_sweeper() -> None:
    """Launch the session reaper (daemon — dies with the process)."""
    threading.Thread(target=_sweep_forever, daemon=True).start()


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
        on_event=relay_sink(_log_event),
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
        on_event=relay_sink(_log_event),
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
    """lock → load → diff → commit confirmed → unlock.

    ``confirm_now=True`` finalises immediately and closes the session.
    ``confirm_now=False`` HOLDS the session for the confirmed-commit
    window: the caller validates (JSNAPy) and then POSTs
    ``/session/{id}/confirm`` or ``/session/{id}/rollback`` — the
    confirming commit must come from this same NETCONF session.

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
        on_event=relay_sink(_log_event),
    )
    held = False
    try:
        jc.connect()
        diff = jc.push_config(
            req.payload,
            fmt=req.fmt,
            mode=req.mode,
            confirm_minutes=req.confirm_minutes,
            comment=req.comment,
        )
        if req.confirm_now:
            jc.confirm_commit()
            confirmed = True
        else:
            sessions.hold(jc, ttl_seconds=req.confirm_minutes * 60)
            held = True
            confirmed = False
    except Restor8Error as exc:
        raise _device_error(exc) from exc
    except Exception as exc:
        raise _unmapped_error(req.host, exc) from exc
    finally:
        if not held:
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


# ── held-session lifecycle (companion to /push confirm_now=false) ──────


class SessionActionResponse(BaseModel):
    """Outcome of a confirm/rollback on a held session."""

    session_id: str
    action: str
    diff: str = ""
    """Empty for confirm; the rollback diff (old ← new) for rollback."""


@app.get("/session/{session_id}")
def session_status(session_id: str) -> dict[str, object]:
    """Status of a held session (host + remaining window).

    Raises:
        HTTPException 404: unknown/expired session.
    """
    status = sessions.peek(session_id)
    if status is None:
        raise HTTPException(404, f"session {session_id} not held (unknown or expired)")
    return status


@app.post("/session/{session_id}/confirm", response_model=SessionActionResponse)
def session_confirm(session_id: str) -> SessionActionResponse:
    """Finalise a held confirmed-commit (validation passed).

    Raises:
        HTTPException 404: unknown/expired session — if the window lapsed,
            the device already self-reverted.
    """
    jc = sessions.take(session_id)
    if jc is None:
        raise HTTPException(404, f"session {session_id} not held (window expired?)")
    try:
        jc.confirm_commit()
    except Restor8Error as exc:
        jc.close()
        raise _device_error(exc) from exc
    jc.close()
    return SessionActionResponse(session_id=session_id, action="confirm")


@app.post("/session/{session_id}/rollback", response_model=SessionActionResponse)
def session_rollback(session_id: str) -> SessionActionResponse:
    """Roll a held confirmed-commit back to the previous config.

    The permanent-commit rollback lives in JunosConnection.rollback()
    (restores the previously-committed known-good config).

    Raises:
        HTTPException 404: unknown/expired session.
    """
    jc = sessions.take(session_id)
    if jc is None:
        raise HTTPException(404, f"session {session_id} not held (window expired?)")
    try:
        diff = jc.rollback()
    except Restor8Error as exc:
        jc.close()
        raise _device_error(exc) from exc
    jc.close()
    return SessionActionResponse(session_id=session_id, action="rollback", diff=diff)


# ── validation snapshots ──────────────────────────────────────────────


class SnapshotRequest(ConnectRequest):
    """Pull one operational RPC reply as XML (JSNAPy snapshot material)."""

    rpc: str = Field(
        default="get_bgp_summary_information",
        description="Junos RPC name — get_bgp_summary_information "
        "(show bgp summary), get_interface_information (show interfaces), …",
    )
    args: dict[str, str] = Field(
        default_factory=dict,
        description="RPC arguments, e.g. {\"terse\": \"true\"}",
    )


class SnapshotResponse(BaseModel):
    """The RPC reply as an XML string."""

    session_id: str
    rpc: str
    xml: str


@app.post("/snapshot", response_model=SnapshotResponse)
def snapshot(req: SnapshotRequest) -> SnapshotResponse:
    """Open a session, run one operational RPC, close.

    Restore (Phase 3) snapshots pre/post state around a config push for
    JSNAPy validation; scenario (Phase 5) polls convergence the same way.

    Raises:
        HTTPException 422: no resolvable credentials.
        HTTPException 502: typed device error (unknown RPC included).
    """
    user, auth = _resolve_creds(req)
    if not user or not auth:
        raise HTTPException(status_code=422, detail="no credentials (see /connect)")
    jc = JunosConnection(
        req.host, user, auth, port=req.port, timeout=req.timeout,
        on_event=relay_sink(_log_event),
    )
    try:
        jc.connect()
        xml = jc.rpc(req.rpc, **req.args)
    except Restor8Error as exc:
        raise _device_error(exc) from exc
    except Exception as exc:
        raise _unmapped_error(req.host, exc) from exc
    finally:
        jc.close()
    return SnapshotResponse(session_id=jc.session_id, rpc=req.rpc, xml=xml)


# ── credential profile management (k8s Secrets, live) ─────────────────


class CredentialProfile(BaseModel):
    """A named credential profile — write-only password."""

    name: str = Field(description="profile name; Secret becomes lab-auth-<name>")
    user: str = Field(description="SSH username")
    password: str = Field(description="SSH password (write-only — never listed)", exclude=True)


class CredentialProfileInfo(BaseModel):
    """What listing shows: identity, never the secret."""

    name: str
    user: str


def _secret_name(profile: str) -> str:
    """Profile → full Secret name (lab-auth prefix enforced for RBAC scope)."""
    name = profile if profile.startswith(PROFILE_PREFIX) else f"{PROFILE_PREFIX}-{profile}"
    if "/" in name or " " in name:
        raise HTTPException(422, "profile names: letters, digits, dashes only")
    return name


@app.get("/credentials", response_model=list[CredentialProfileInfo])
def list_credentials() -> list[CredentialProfileInfo]:
    """All credential profiles (usernames only — passwords are write-only)."""
    headers = _k8s_headers()
    if headers is None:
        return []
    ns = (_SA / "namespace").read_text().strip() if (_SA / "namespace").exists() else "restor8"
    ca = str(_SA / "ca.crt") if (_SA / "ca.crt").exists() else False
    r = httpx.get(
        f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/secrets",
        headers=headers,
        verify=ca,
        timeout=10,
    )
    if r.status_code != 200:
        raise HTTPException(502, f"k8s API {r.status_code}: {r.text[:150]}")
    out = []
    for s in r.json().get("items", []):
        name = s["metadata"]["name"]
        if not name.startswith(PROFILE_PREFIX):
            continue
        user = base64.b64decode(s.get("data", {}).get("LAB_USER", "")).decode() if s.get("data", {}).get("LAB_USER") else ""
        out.append(CredentialProfileInfo(name=name, user=user))
    return sorted(out, key=lambda p: p.name)


@app.post("/credentials", response_model=CredentialProfileInfo, status_code=201)
def upsert_credential(profile: CredentialProfile) -> CredentialProfileInfo:
    """Create or rotate a profile — effective IMMEDIATELY (no restart).

    Args:
        profile: name, username, password. Existing profile with the same
            name is updated in place (rotation).
    """
    headers = _k8s_headers()
    if headers is None:
        raise HTTPException(501, "not running in-cluster (no service account)")
    ns = (_SA / "namespace").read_text().strip() if (_SA / "namespace").exists() else "restor8"
    ca = str(_SA / "ca.crt") if (_SA / "ca.crt").exists() else False
    secret = _secret_name(profile.name)
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret, "namespace": ns, "labels": {"restor8.io/credential": "true"}},
        "type": "Opaque",
        "data": {
            "LAB_USER": base64.b64encode(profile.user.encode()).decode(),
            "LAB_PASSWORD": base64.b64encode(profile.password.encode()).decode(),
        },
    }
    base = f"https://kubernetes.default.svc/api/v1/namespaces/{ns}/secrets"
    created = httpx.post(base, headers=headers, json=body, verify=ca, timeout=10)
    if created.status_code == 409:  # exists → rotate in place
        rotated = httpx.patch(
            f"{base}/{secret}",
            headers={**headers, "Content-Type": "application/merge-patch+json"},
            json={"data": body["data"]},
            verify=ca,
            timeout=10,
        )
        if rotated.status_code not in (200, 201):
            raise HTTPException(502, f"rotation failed ({rotated.status_code}): {rotated.text[:150]}")
    elif created.status_code not in (200, 201):
        raise HTTPException(502, f"create failed ({created.status_code}): {created.text[:150]}")
    return CredentialProfileInfo(name=secret, user=profile.user)
