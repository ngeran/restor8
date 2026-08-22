"""restore — push a historical config back to a device, behind a gate (Phase 3).

The safety model (locked decision, TODO.md): a HUMAN approves the push
(``?approve=true`` — the UI's confirm button); once pushed, automation
owns the outcome — the push rides a confirmed-commit window held open by
connector, post-state is validated, and a failed validation rolls the
device back automatically. No unattended restores, no sitting on a
broken config.

Choreography across services (restore itself touches no devices):

    backup    GET /backup/{id}/config/{sha}     the config to restore
    connector POST /config                      current running (for diff)
    connector POST /push  (confirm_now=false)   override push, session held
    connector POST /snapshot (pre + post)       JSNAPy material
    connector POST /session/{sid}/confirm|rollback
"""

from __future__ import annotations

import difflib
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from restor8_core.jsnapy_runner import ComparisonResult, compare
from restor8_core.jsonlog import setup_logging

# JSON lines from the first log call on (no root logging config existed).
setup_logging("restore")

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://restor8-inventory:8080")
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://restor8-connector:8080")
BACKUP_URL = os.environ.get("BACKUP_URL", "http://restor8-backup:8080")

app = FastAPI(
    title="restor8 restore",
    description="Diff vs Git history; gated confirmed-commit restore with JSNAPy validation.",
    version="0.1.0",
)


class RestoreRequest(BaseModel):
    """Optional knobs for a restore push."""

    testdef: dict[str, object] | None = Field(
        default=None,
        description="JSNAPy test-file mapping — when given, pre/post "
        "snapshots of `rpc` are compared; when omitted, the default "
        "config-match check (post-running == restored config) applies",
    )
    rpc: str = Field(
        default="get_bgp_summary_information",
        description="snapshot RPC for the JSNAPy check (show bgp summary)",
    )
    fmt: str = Field(default="text", description="config format (keep per-device stable)")
    confirm_minutes: int = Field(default=5, description="confirmed-commit window for validation")


class ValidationInfo(BaseModel):
    """What was checked and how it went."""

    check: str
    """`jsnapy` or `config-match`."""

    passed: bool
    results: list[dict[str, str]] = Field(default_factory=list)
    """JSNAPy per-test entries (empty for config-match)."""


class RestoreResult(BaseModel):
    """Final outcome of a restore attempt."""

    device: str
    sha: str
    restored: bool
    """True only if pushed AND validated AND confirmed."""

    validation: ValidationInfo
    rollback_diff: str = Field(default="", description="present when rolled back")


# ── helpers ────────────────────────────────────────────────────────────


def _device(device_id: int) -> dict[str, object]:
    """Inventory lookup → 404/502 with cause."""
    try:
        r = httpx.get(f"{INVENTORY_URL}/devices/{device_id}", timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"inventory unreachable: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, f"device {device_id} not found")
    if r.status_code != 200:
        raise HTTPException(502, f"inventory error {r.status_code}")
    return r.json()


def _call(
    url: str, json_: dict[str, object] | None = None, *, post: bool = False
) -> httpx.Response:
    """HTTP helper mapping connector/backup failures to 502 passthrough.

    ``post=True`` forces POST for body-less calls (confirm/rollback) —
    without it they'd GET a POST-only route and surface as a confusing
    wrapped "Method Not Allowed".
    """
    try:
        if json_ is not None or post:
            r = httpx.post(url, json=json_, timeout=300)
        else:
            r = httpx.get(url, timeout=300)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unreachable ({url}): {exc}") from exc
    if r.status_code >= 400:
        detail = None
        try:
            detail = r.json().get("detail")
        except ValueError:
            pass
        raise HTTPException(502, detail or f"upstream {r.status_code} from {url}")
    return r


def _config_at(device_id: int, sha: str) -> dict[str, object]:
    """backup's config@sha (+ resolves 'latest')."""
    return _call(f"{BACKUP_URL}/backup/{device_id}/config/{sha}").json()


def _running(device: dict[str, object], fmt: str) -> str:
    """Current running config via connector."""
    return _call(
        f"{CONNECTOR_URL}/config",
        {
            "host": device["mgmt_ip"],
            "port": device["port"],
            "auth_ref": device["auth_ref"],
            "fmt": fmt,
        },
    ).json()["config"]


def _normalized(config: str) -> list[str]:
    """Lines for comparison: strip the volatile '## Last changed' stamp."""
    return [
        line.rstrip()
        for line in config.splitlines()
        if line.strip() and not line.startswith("##")
    ]


# ── endpoints ──────────────────────────────────────────────────────────


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — smoke-test 200 target."""
    return {"service": "restor8-restore", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target."""
    return {"status": "ok"}


@app.get("/restore/{device_id}/diff/{sha}")
def restore_diff(device_id: int, sha: str) -> dict[str, object]:
    """Unified diff: running config vs the backup at ``sha`` (no push).

    Direction: what would change ON THE DEVICE to reach the backup
    (running → target), ready to render in the Phase 7 diff view.
    """
    device = _device(device_id)
    backup = _config_at(device_id, sha)
    # fmt: text — the format backups are stored in (per-device stability, spec §4)
    running = _running(device, "text")
    diff = "\n".join(
        difflib.unified_diff(
            _normalized(running),
            _normalized(str(backup["config"])),
            fromfile=f"{device['name']} (running)",
            tofile=f"{device['name']} @ {backup['sha']}",
            lineterm="",
        )
    )
    changed = sum(
        1
        for line in diff.splitlines()
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    )
    return {"device": device["name"], "sha": backup["sha"], "changed_lines": changed, "diff": diff}


@app.post("/restore/{device_id}/{sha}", response_model=RestoreResult)
def restore_push(
    device_id: int, sha: str, approve: bool = False, body: RestoreRequest | None = None
) -> RestoreResult:
    """Restore a backup: gated push, validation, auto-confirm/rollback.

    Args:
        device_id: inventory ID.
        sha: backup commit (or ``latest``).
        approve: THE manual-approve gate — must be explicitly true.
        body: optional testdef/rpc/fmt/window knobs.

    Returns:
        RestoreResult — ``restored=true`` only on a fully validated,
        confirmed restore; otherwise the device was rolled back
        (``rollback_diff`` shows what was undone).

    Raises:
        HTTPException 403: no approval (includes the diff summary so the
            caller can show it before confirming).
        HTTPException 404/502: unknown device/commit or upstream failure.
    """
    if not approve:
        raise HTTPException(
            403,
            {
                "reason": "restore requires manual approval",
                "hint": "review GET /restore/{device_id}/diff/{sha}, then re-POST with ?approve=true",
            },
        )
    req = body or RestoreRequest()
    device = _device(device_id)
    backup = _config_at(device_id, sha)
    target = str(backup["config"])

    # Optional JSNAPy pre-snapshot (before any change).
    pre_xml: str | None = None
    if req.testdef is not None:
        pre_xml = _call(
            f"{CONNECTOR_URL}/snapshot",
            {
                "host": device["mgmt_ip"],
                "port": device["port"],
                "auth_ref": device["auth_ref"],
                "rpc": req.rpc,
            },
        ).json()["xml"]

    # Push through the confirmed-commit pipeline; hold the session.
    push = _call(
        f"{CONNECTOR_URL}/push",
        {
            "host": device["mgmt_ip"],
            "port": device["port"],
            "auth_ref": device["auth_ref"],
            "payload": target,
            "fmt": req.fmt,
            "mode": "override",
            "confirm_minutes": req.confirm_minutes,
            "comment": f"restor8: restore {device['name']} @ {backup['sha']}",
            "confirm_now": False,
        },
    ).json()
    session_id = str(push["session_id"])

    # ── validate before the window closes ──
    validation: ValidationInfo
    try:
        if req.testdef is not None:
            post_xml = _call(
                f"{CONNECTOR_URL}/snapshot",
                {
                    "host": device["mgmt_ip"],
                    "port": device["port"],
                    "auth_ref": device["auth_ref"],
                    "rpc": req.rpc,
                },
            ).json()["xml"]
            cmp: ComparisonResult = compare(req.testdef, pre_xml or "", str(post_xml))
            validation = ValidationInfo(check="jsnapy", passed=cmp.passed, results=cmp.results)
        else:
            running_now = _running(device, req.fmt)
            passed = _normalized(running_now) == _normalized(target)
            validation = ValidationInfo(check="config-match", passed=passed)
    except HTTPException:
        # Validation itself blew up → treat as failed validation; the
        # rollback below still fires, so the device never keeps an
        # unvalidated restore.
        validation = ValidationInfo(check="error", passed=False)

    # ── confirm or roll back on the SAME held session ──
    if validation.passed:
        _call(f"{CONNECTOR_URL}/session/{session_id}/confirm", post=True)
        return RestoreResult(
            device=str(device["name"]),
            sha=str(backup["sha"]),
            restored=True,
            validation=validation,
        )
    rollback = _call(f"{CONNECTOR_URL}/session/{session_id}/rollback", post=True).json()
    return RestoreResult(
        device=str(device["name"]),
        sha=str(backup["sha"]),
        restored=False,
        validation=validation,
        rollback_diff=str(rollback.get("diff", "")),
    )
