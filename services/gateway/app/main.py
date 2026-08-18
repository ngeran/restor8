"""gateway — the browser's single door into restor8 (Phase 6).

Two jobs:

1. **Live feedback** — connector and scenario push their progress events
   here (``POST /internal/events``); this service fans them out to
   WebSocket subscribers, optionally filtered by session/device/run.
   The browser never polls: a scenario run streams its phases as they
   happen, a restore streams its push pipeline stage by stage.
2. **REST aggregation** — thin proxies over the service APIs the UI
   needs, so the frontend has one origin and one URL scheme (/api/…)
   and CORS never exists.

Why a bus and not direct WS to connector: sessions and runs are owned by
different services, but the UX wants ONE stream ("everything happening on
device p3 right now"). Fan-in here, fan-out here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

log = logging.getLogger("restor8.gateway")
logging.basicConfig(level=logging.INFO, format="%(message)s")

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://restor8-inventory:8080")
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://restor8-connector:8080")
BACKUP_URL = os.environ.get("BACKUP_URL", "http://restor8-backup:8080")
RESTORE_URL = os.environ.get("RESTORE_URL", "http://restor8-restore:8080")
TOPOLOGY_URL = os.environ.get("TOPOLOGY_URL", "http://restor8-topology:8080")
SCENARIO_URL = os.environ.get("SCENARIO_URL", "http://restor8-scenario:8080")

app = FastAPI(
    title="restor8 gateway",
    description="REST aggregation + WebSocket live-feedback fan-out.",
    version="0.1.0",
)

# ── event bus ──────────────────────────────────────────────────────────
#
# Subscribers are asyncio.Queues with optional filters. Ingest is an
# async POST; fan-out happens per-subscriber in each WS handler's loop.
# Bounded queues + drop-oldest: a slow browser must never back-pressure
# a device operation.


class Bus:
    """In-memory pub/sub for progress events."""

    def __init__(self) -> None:
        self._subs: dict[int, tuple[asyncio.Queue[dict[str, Any]], dict[str, str]]] = {}
        self._next = 0

    def subscribe(self, filters: dict[str, str]) -> asyncio.Queue[dict[str, Any]]:
        """Register a subscriber; returns its bounded queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._next += 1
        self._subs[self._next] = (q, filters)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a subscriber by queue identity."""
        for sid, (sq, _) in list(self._subs.items()):
            if sq is q:
                del self._subs[sid]
                break

    def publish(self, event: dict[str, Any]) -> None:
        """Fan one event out to every matching subscriber (drop-oldest)."""
        for q, f in list(self._subs.values()):
            if f:
                ok = all(str(event.get(k, "")) == v for k, v in f.items())
                if not ok:
                    continue
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # drop oldest, keep newest
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


bus = Bus()


@app.post("/internal/events", status_code=202)
async def ingest(event: dict[str, Any]) -> dict[str, bool]:
    """Ingest one progress event (connector device events, scenario phases).

    The body IS the DeviceEvent (or scenario phase record); unknown
    shapes pass through untouched — the UI renders what it recognises.
    """
    bus.publish(event)
    return {"queued": True}


@app.websocket("/ws")
async def ws_root(ws: WebSocket) -> None:
    """Live event stream.

    Query filters (all optional, ANDed): ``?session=<id> &device=<name>
    &run=<id>``. Without filters, every event flows.
    """
    await ws.accept()
    filters: dict[str, str] = {}
    for key in ("session", "device", "run"):
        val = ws.query_params.get(key)
        if val:
            filters[key] = val
    q = bus.subscribe(filters)
    try:
        while True:
            event = await q.get()
            await ws.send_text(json.dumps(event, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)


# ── REST aggregation (GET-only proxies + the two UI actions) ───────────


async def _proxy(url: str) -> Any:
    """GET an upstream and pass its JSON (or its error) through."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise HTTPException(r.status_code, detail or f"upstream {r.status_code}")
    return r.json()


@app.get("/")
def index() -> dict[str, str]:
    """Service banner."""
    return {"service": "restor8-gateway", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Probe target."""
    return {"status": "ok"}


@app.get("/api/summary")
async def summary() -> dict[str, Any]:
    """Dashboard bundle in one round-trip: devices + topology + scenarios."""
    devices, topo, scenarios = await asyncio.gather(
        _proxy(f"{INVENTORY_URL}/devices"),
        _proxy(f"{TOPOLOGY_URL}/topology"),
        _proxy(f"{SCENARIO_URL}/scenarios"),
    )
    return {"devices": devices, "topology": topo, "scenarios": scenarios}


@app.get("/api/devices")
async def devices() -> Any:
    """Inventory rows."""
    return await _proxy(f"{INVENTORY_URL}/devices")


@app.get("/api/topology")
async def topology() -> Any:
    """The topology plan (nodes + links)."""
    return await _proxy(f"{TOPOLOGY_URL}/topology")


@app.get("/api/devices/{device_id}/backups")
async def backups(device_id: int) -> Any:
    """Backup history for a device."""
    return await _proxy(f"{BACKUP_URL}/backup/{device_id}/history")


@app.post("/api/devices/{device_id}/backup")
async def backup_now(device_id: int) -> Any:
    """Run a backup right now (UI button)."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BACKUP_URL}/backup/{device_id}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.post("/api/devices/{device_id}/restore/{sha}")
async def restore_now(device_id: int, sha: str, approve: bool = False) -> Any:
    """Restore a device to a backup (manual-approve gate enforced here too)."""
    if not approve:
        raise HTTPException(403, {"reason": "restore requires approval", "hint": "?approve=true"})
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{RESTORE_URL}/restore/{device_id}/{sha}?approve=true")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.get("/api/labs")
async def labs() -> Any:
    """Lab catalogue (grouped by family)."""
    return await _proxy(f"{CONFIG_URL}/labs")


@app.post("/api/labs/{name}/apply")
async def apply_lab(name: str) -> Any:
    """Apply/restore a whole lab (sequential device pushes)."""
    async with httpx.AsyncClient(timeout=900) as client:
        r = await client.post(f"{CONFIG_URL}/labs/{name}/apply")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.get("/api/devices/{device_id}/diff/{sha}")
async def diff(device_id: int, sha: str) -> Any:
    """Unified diff: running vs the backup at sha (restore's endpoint)."""
    return await _proxy(f"{RESTORE_URL}/restore/{device_id}/diff/{sha}")


@app.get("/api/scenarios")
async def scenarios() -> Any:
    """Available scenario definitions."""
    return await _proxy(f"{SCENARIO_URL}/scenarios")


@app.get("/api/runs/{run_id}")
async def run(run_id: int) -> Any:
    """One scenario run (status + detail)."""
    return await _proxy(f"{SCENARIO_URL}/scenario/run/{run_id}")


@app.get("/api/runs")
async def runs() -> Any:
    """Recent runs across scenarios — via the known scenario list."""
    names = await _proxy(f"{SCENARIO_URL}/scenarios")
    out: list[Any] = []
    for s in names:
        out.extend(await _proxy(f"{SCENARIO_URL}/scenario/{s['name']}/runs"))
    out.sort(key=lambda r: r.get("id", 0), reverse=True)
    return out[:50]


@app.post("/api/scenarios/{name}/run")
async def start_run(name: str) -> Any:
    """Start a scenario run from the UI (returns run id immediately)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{SCENARIO_URL}/scenario/{name}/run")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


# ── config editor (Configure pillar) ───────────────────────────────────

CONFIG_URL = os.environ.get("CONFIG_URL", "http://restor8-config:8080")


@app.get("/api/config/templates")
async def config_templates() -> Any:
    """Template catalogue with form schemas."""
    return await _proxy(f"{CONFIG_URL}/templates")


@app.post("/api/config/templates/{name}/render")
async def config_render(name: str, body: dict[str, Any]) -> Any:
    """Dry-run render (payload preview; nothing pushed).

    ``body`` is the full request object (``{"values": {...}}``) passed
    through verbatim to the config service.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{CONFIG_URL}/templates/{name}/render", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.get("/api/config/devices/{device_id}/running")
async def config_running(device_id: int, fmt: str = "set") -> Any:
    """Read a device's running config (set or text format)."""
    return await _proxy(f"{CONFIG_URL}/devices/{device_id}/running?fmt={fmt}")


@app.post("/api/config/push")
async def config_push(body: dict[str, Any]) -> Any:
    """Push a reviewed payload (long timeout: device commit on the wire)."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{CONFIG_URL}/push", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()
