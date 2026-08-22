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
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from restor8_core.jsonlog import setup_logging

log = logging.getLogger("restor8.gateway")
setup_logging("gateway")

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


@app.post("/api/devices")
async def create_device(body: dict[str, Any]) -> Any:
    """Register a device (UI form)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{INVENTORY_URL}/devices", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: int, body: dict[str, Any]) -> Any:
    """Patch a device (UI form)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.patch(f"{INVENTORY_URL}/devices/{device_id}", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.delete("/api/devices/{device_id}", status_code=204)
async def delete_device(device_id: int) -> None:
    """Remove a device (UI form)."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(f"{INVENTORY_URL}/devices/{device_id}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))


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


# ── held confirmed-commit sessions (§3: the two-phase safety window) ───


@app.get("/api/credentials")
async def credentials() -> Any:
    """Credential profiles (usernames only)."""
    return await _proxy(f"{CONNECTOR_URL}/credentials")


@app.post("/api/credentials")
async def upsert_credential(body: dict[str, Any]) -> Any:
    """Create/rotate a credential profile — effective immediately."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{CONNECTOR_URL}/credentials", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


# ── live interface state (topology hover source) ────────────────────────
#
# Fan-out snapshots of every device's interface table through connector,
# parsed to {iface: {addrs, oper}} and cached 30s — hovers read the cache,
# never the wire. Whitespace-tolerant parsing throughout: cRPD puts
# newlines inside its XML tags (the recurring theme).

_iface_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _parse_interfaces(xml: str) -> dict[str, dict[str, Any]]:
    """terse interface-information XML → {iface: {addrs: [...], oper}}}."""
    out: dict[str, dict[str, Any]] = {}
    for block in re.findall(r"<physical-interface>([\s\S]*?)</physical-interface>", xml):
        m = re.search(r"<name>\s*([^\s<]+)", block)
        name = m.group(1) if m else ""
        oper = re.search(r"<oper-status>\s*(\S+)", block)
        addrs = [
            a for a in re.findall(r"<ifa-local>\s*([\d.]+/\d+)\s*</ifa-local>", block)
            if ":" not in a  # skip inet6
        ]
        out[name] = {"addrs": addrs, "oper": oper.group(1) if oper else None}
    return out


@app.get("/api/interfaces")
async def interfaces() -> Any:
    """Live interface table for every device (30s cache, fan-out snapshot).

    Devices that fail are reported with their error, not dropped — the
    hover shows "unreachable" rather than stale-guessing.
    """
    cached = _iface_cache.get("all")
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]

    devices = await _proxy(f"{INVENTORY_URL}/devices")

    async def one(dev: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    f"{CONNECTOR_URL}/snapshot",
                    json={
                        "host": dev["mgmt_ip"],
                        "port": dev["port"],
                        "auth_ref": dev["auth_ref"],
                        "rpc": "get_interface_information",
                        "args": {"terse": "true"},
                    },
                )
            if r.status_code != 200:
                return dev["name"], {"error": str(r.json().get("detail", {}).get("error", r.status_code))}
            return dev["name"], {"interfaces": _parse_interfaces(r.json()["xml"])}
        except Exception as exc:  # noqa: BLE001 — one dead device ≠ dead endpoint
            return dev["name"], {"error": str(exc)[:120]}

    results = await asyncio.gather(*(one(d) for d in devices))
    payload = {"at": time.time(), "devices": dict(results)}
    _iface_cache["all"] = (time.monotonic(), payload)
    return payload


# ── topology discovery: the lab inferred FROM live configuration ────────
#
# The objective (validated 2026-08-22): the diagram must be built from
# what the devices actually run, not from a hand-maintained plan. Links
# are inferred by segment math over live interface tables: two devices
# holding host addresses of the same /30 are connected. No plan involved.


def _ip_to_int(ip: str) -> int:
    parts = ip.split(".")
    return (int(parts[0]) << 24) + (int(parts[1]) << 16) + (int(parts[2]) << 8) + int(parts[3])


def _discover_links(devices_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer p2p links: same /30, different devices (or loop-pairs)."""
    # segment -> [(device, iface, ip)] for every IPv4 address that isn't
    # mgmt (eth0/172.20 bridge) or loopback (lo/127.*)
    by_segment: dict[int, list[tuple[str, str, str]]] = {}
    for dev, info in devices_state.items():
        for iface, data in (info.get("interfaces") or {}).items():
            if iface in ("lo", "eth0"):
                continue
            for addr in data.get("addrs", []):
                ip, _, plen = addr.partition("/")
                if plen != "30":
                    continue  # only p2p /30s form links (lab convention)
                net = _ip_to_int(ip) & 0xFFFFFFFC
                by_segment.setdefault(net, []).append((dev, iface, ip))

    links: list[dict[str, Any]] = []
    for net, ends in by_segment.items():
        uniq = {(d, i) for d, i, _ in ends}
        seg_ip = f"{(net >> 24) & 255}.{(net >> 16) & 255}.{(net >> 8) & 255}.0/30"
        if len(uniq) == 2:
            (da, ia, *_), (db, ib, *_) = sorted(uniq)
            ipa = next(ip for d, i, ip in ends if d == da and i == ia)
            ipb = next(ip for d, i, ip in ends if d == db and i == ib)
            links.append(
                {"a": da, "a_if": ia, "a_ip": f"{ipa}/30",
                 "b": db, "b_if": ib, "b_ip": f"{ipb}/30",
                 "segment": seg_ip, "state": "up"}
            )
        elif len(uniq) == 1:
            # configured but unpaired — a dangling link: real drift signal
            dev, iface = next(iter(uniq))
            ip = next(ip for d, i, ip in ends if d == dev and i == iface)
            links.append(
                {"a": dev, "a_if": iface, "a_ip": f"{ip}/30",
                 "b": None, "b_if": None, "b_ip": None,
                 "segment": seg_ip, "state": "dangling"}
            )
    return links


@app.get("/api/topology/discover")
async def discover() -> Any:
    """Discovered lab map: nodes from inventory, links FROM live configs.

    Reads the same 30s interface cache the hover uses; unreachable
    devices appear as nodes with ``state: unreachable`` so the map never
    silently shrinks.
    """
    devices = await _proxy(f"{INVENTORY_URL}/devices")
    cached = _iface_cache.get("all")
    live = cached[1]["devices"] if cached and time.monotonic() - cached[0] < 60 else None
    if live is None:
        live = (await interfaces())["devices"]

    nodes = []
    for d in devices:
        state = live.get(d["name"], {})
        nodes.append({
            "name": d["name"], "id": d["id"], "platform": d["platform"],
            "mgmt": d["mgmt_ip"],
            "state": "unreachable" if "error" in state else "up",
        })
    return {
        "at": time.time(),
        "nodes": nodes,
        "links": _discover_links({name: s for name, s in live.items() if "error" not in s}),
    }


@app.get("/api/session/{session_id}")
async def session_status(session_id: str) -> Any:
    """Status of a held session: host + seconds left in the window."""
    return await _proxy(f"{CONNECTOR_URL}/session/{session_id}")


@app.post("/api/session/{session_id}/confirm")
async def session_confirm(session_id: str) -> Any:
    """Finalise a confirmed commit (validation passed / human is sure)."""
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{CONNECTOR_URL}/session/{session_id}/confirm")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


@app.post("/api/session/{session_id}/rollback")
async def session_rollback(session_id: str) -> Any:
    """Roll a confirmed commit back to the previous config."""
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{CONNECTOR_URL}/session/{session_id}/rollback")
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


@app.post("/api/scenarios/{name}/render")
async def render_scenario(name: str) -> Any:
    # Dry-run: rendered per-target config for a scenario, nothing pushed.
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{SCENARIO_URL}/scenario/{name}/render")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail"))
    return r.json()


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
