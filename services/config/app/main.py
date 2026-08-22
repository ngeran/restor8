"""config — the Configure pillar's engine: forms → render → push.

Why this service exists (spec pillar 1): pushing protocol/feature config
from reusable templates is half of restor8's point. Each feature ships as
a PAIR of checked-in files — a Jinja2 template (set-format, the only
format that carries deletes safely) and a form schema the frontend turns
into input fields — so the UI renders real forms with zero hardcoded
knowledge of Junos features. Everything device-touching goes through
connector; rendering happens here with the SAME engine the scenario
service uses.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field

from restor8_core.jsonlog import setup_logging

# JSON lines from the first log call on (no root logging config existed).
setup_logging("config")

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://restor8-inventory:8080")
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://restor8-connector:8080")
TOPOLOGY_URL = os.environ.get("TOPOLOGY_URL", "http://restor8-topology:8080")

_TPL_DIR = Path(__file__).parent / "templates"
_LAB_DIR = Path(__file__).parent / "labs"
_ENV = Environment(
    loader=FileSystemLoader(str(_TPL_DIR)),
    undefined=StrictUndefined,  # missing var = loud failure, never silent ""
    trim_blocks=True,
    lstrip_blocks=True,
)

app = FastAPI(
    title="restor8 config",
    description="Template-driven configuration: forms → render → preview → push.",
    version="0.1.0",
)


def _schema(name: str) -> dict[str, Any]:
    """Load one template's form schema (404 for unknown names)."""
    f = _TPL_DIR / f"{name}.yml"
    if not f.exists():
        raise HTTPException(404, f"unknown template '{name}'")
    data = yaml.safe_load(f.read_text())
    if not isinstance(data, dict) or "fields" not in data:
        raise HTTPException(500, f"malformed schema for '{name}'")
    return data


class RenderRequest(BaseModel):
    """Form values for a dry-run render."""

    values: dict[str, Any] = Field(default_factory=dict)


class RenderResponse(BaseModel):
    """The rendered payload (set-format) — shown for review before pushing."""

    payload: str
    mode: str


class PushRequest(BaseModel):
    """A reviewed payload bound for one device.

    Comes from a rendered template or the editor's raw textarea; either way
    the user SAW the exact lines before this call.
    """

    device_id: int
    payload: str = Field(description="set-format config lines")
    mode: str = Field(default="merge", description="merge | override")
    fmt: str = Field(default="set", description="set (recommended) | text")
    comment: str = Field(default="restor8-ui", description="Junos commit comment")
    confirm_now: bool = Field(
        default=True,
        description="finalise immediately; false keeps the confirmed-commit window (5 min)",
    )


class PushResponse(BaseModel):
    """What the device reported: the applied diff + commit state."""

    session_id: str
    diff: str
    confirmed: bool


def _device(device_id: int) -> dict[str, Any]:
    """Inventory row → 404/502 with cause."""
    try:
        r = httpx.get(f"{INVENTORY_URL}/devices/{device_id}", timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"inventory unreachable: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, f"device {device_id} not found")
    if r.status_code != 200:
        raise HTTPException(502, f"inventory error {r.status_code}")
    return r.json()


@app.get("/")
def index() -> dict[str, str]:
    """Service banner."""
    return {"service": "restor8-config", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Probe target."""
    return {"status": "ok"}


@app.get("/templates")
def templates() -> list[dict[str, Any]]:
    """All template schemas — the frontend's form catalogue."""
    out = []
    for f in sorted(_TPL_DIR.glob("*.yml")):
        out.append(yaml.safe_load(f.read_text()))
    return out


@app.post("/templates/{name}/render", response_model=RenderResponse)
def render(name: str, req: RenderRequest) -> RenderResponse:
    """Dry-run: render a template with form values. Nothing is pushed.

    Raises:
        HTTPException 404: unknown template.
        HTTPException 422: missing required value (StrictUndefined).
    """
    schema = _schema(name)
    try:
        payload = _ENV.get_template(f"{name}.j2").render(**req.values)
    except Exception as exc:  # jinja2.UndefinedError & friends
        raise HTTPException(422, f"render failed: {exc}") from exc
    return RenderResponse(payload=payload.strip() + "\n", mode=str(schema.get("mode", "merge")))


@app.get("/devices/{device_id}/running")
def running(device_id: int, fmt: str = "set") -> dict[str, str]:
    """Read a device's running config (via connector — read-only)."""
    dev = _device(device_id)
    try:
        r = httpx.post(
            f"{CONNECTOR_URL}/config",
            json={
                "host": dev["mgmt_ip"],
                "port": dev["port"],
                "auth_ref": dev["auth_ref"],
                "fmt": fmt,
            },
            timeout=180,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"connector unreachable: {exc}") from exc
    if r.status_code != 200:
        raise HTTPException(502, r.json().get("detail"))
    return {"device": str(dev["name"]), "fmt": fmt, "config": r.json()["config"]}


@app.post("/push", response_model=PushResponse)
def push(req: PushRequest) -> PushResponse:
    """Push a reviewed payload through connector's confirmed-commit pipeline.

    The connector relays every stage (locking → loading → diff → commit)
    to the gateway — the UI's live feed shows it as it happens.

    Raises:
        HTTPException 404: unknown device.
        HTTPException 502: connector/device error (Junos text intact).
    """
    dev = _device(req.device_id)
    try:
        r = httpx.post(
            f"{CONNECTOR_URL}/push",
            json={
                "host": dev["mgmt_ip"],
                "port": dev["port"],
                "auth_ref": dev["auth_ref"],
                "payload": req.payload,
                "fmt": req.fmt,
                "mode": req.mode,
                "comment": req.comment,
                "confirm_minutes": 5,
                "confirm_now": req.confirm_now,
            },
            timeout=300,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"connector unreachable: {exc}") from exc
    if r.status_code != 200:
        raise HTTPException(502, r.json().get("detail"))
    out = r.json()
    return PushResponse(
        session_id=str(out["session_id"]), diff=str(out["diff"]), confirmed=bool(out["confirmed"])
    )


# ── labs: whole-fleet configuration sets ───────────────────────────────
#
# A lab is the full configuration needed across ALL devices to run one
# exercise (MPLS, L3VPN, TWAMP…). Steps select devices by plan role (or
# explicitly) and render a template with static values. Applying is
# idempotent — set-format convergence — so "apply the lab" IS "restore
# the lab": whatever drifted gets overwritten back to the lab's lines.


class LabNodeResult(BaseModel):
    """Per-device outcome of a lab apply."""

    device: str
    ok: bool
    diff_lines: int = 0
    error: str = ""


class LabApplyResult(BaseModel):
    """Aggregate outcome."""

    lab: str
    applied: int
    failed: int
    nodes: list[LabNodeResult]


@app.get("/labs")
def labs() -> list[dict[str, Any]]:
    """The lab catalogue (grouped by family in the UI)."""
    out = []
    for f in sorted(_LAB_DIR.glob("*.yml")):
        out.append(yaml.safe_load(f.read_text()))
    return out


@app.post("/labs/{name}/apply", response_model=LabApplyResult)
def apply_lab(name: str) -> LabApplyResult:
    """Render + push every step of a lab, sequentially, device by device.

    Sequential on purpose: lab-scale, and the live event feed reads in
    apply order. Each push is confirmed-commit finalised (immediate);
    failures stop the run with the offending device recorded.

    Raises:
        HTTPException 404: unknown lab.
        HTTPException 502: topology/inventory unreachable.
    """
    f = _LAB_DIR / f"{name}.yml"
    if not f.exists():
        raise HTTPException(404, f"unknown lab '{name}'")
    lab = yaml.safe_load(f.read_text())

    try:
        plan = httpx.get(f"{TOPOLOGY_URL}/topology", timeout=15).json()
        devices = {d["name"]: d for d in httpx.get(f"{INVENTORY_URL}/devices", timeout=15).json()}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"plan/inventory unavailable: {exc}") from exc

    # step targets: by role (from the plan) or explicit device names
    results: list[LabNodeResult] = []

    def _render_and_push(dev: dict[str, Any], template: str, values: dict[str, Any]) -> LabNodeResult:
        try:
            payload = _ENV.get_template(f"{template}.j2").render(**values)
        except Exception as exc:  # noqa: BLE001 — record, keep going
            return LabNodeResult(device=str(dev["name"]), ok=False, error=f"render: {exc}")
        try:
            r = httpx.post(
                f"{CONNECTOR_URL}/push",
                json={
                    "host": dev["mgmt_ip"],
                    "port": dev["port"],
                    "auth_ref": dev["auth_ref"],
                    "payload": payload,
                    "fmt": "set",
                    "mode": "merge",
                    "comment": f"restor8-lab: {name}",
                    "confirm_now": True,
                },
                timeout=300,
            )
        except httpx.HTTPError as exc:
            return LabNodeResult(device=str(dev["name"]), ok=False, error=str(exc))
        if r.status_code != 200:
            detail = r.json().get("detail", {})
            return LabNodeResult(
                device=str(dev["name"]), ok=False,
                error=str(detail.get("message", r.status_code))[:200],
            )
        diff = r.json().get("diff", "")
        changed = sum(
            1
            for line in diff.splitlines()
            if line[:1] in "+-" and line[:3] not in ("+++", "---")
        )
        return LabNodeResult(device=str(dev["name"]), ok=True, diff_lines=changed)

    for step in lab.get("steps", []):
        targets: list[dict[str, Any]] = []
        for n in plan["nodes"]:
            if n["name"] not in devices:
                continue
            if step.get("roles") and n.get("role") in step["roles"]:
                targets.append(devices[n["name"]])
            elif step.get("devices") and n["name"] in step["devices"]:
                targets.append(devices[n["name"]])
        for dev in targets:
            results.append(_render_and_push(dev, str(step["template"]), dict(step.get("values", {}))))

    return LabApplyResult(
        lab=str(lab.get("name", name)),
        applied=sum(1 for x in results if x.ok),
        failed=sum(1 for x in results if not x.ok),
        nodes=results,
    )
