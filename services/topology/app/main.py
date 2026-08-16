"""topology — the lab's intended shape, as code, applied by the app (Phase 4).

Why this service exists: "use the app to configure the lab" is the point
of restor8. The topology definition lives in ``topologies/mpls-core.yml``
(checked into the repo — reviewed like code, baked into the image); this
service reconciles it against inventory and pushes each node's baseline
through connector. Nobody hand-configures a lab device; the file is the
single source of truth.

Phase 4 reshaping (recorded in TODO.md): the original plan was "parse a
containerlab .clab.yml", but the lab turned out to be clabernetes-in-k8s
with all 10 nodes already registered in inventory — so the service starts
from its OWN declarative file and reconciles against inventory instead.
The file's ``underlay`` key records how peerings are realized today
(flat pod network, unicast-only) until a real fabric is wired.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://restor8-inventory:8080")
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://restor8-connector:8080")

_TOPO_DIR = Path(__file__).parent / "topologies"
_TOPO_FILE = _TOPO_DIR / os.environ.get("TOPOLOGY_FILE", "mpls-core.yml")

app = FastAPI(
    title="restor8 topology",
    description="Lab topology plan: reconcile vs inventory, apply baselines via connector.",
    version="0.1.0",
)


def _load() -> dict[str, Any]:
    """Parse the topology plan (fail fast at request time if malformed).

    Returns:
        The YAML as a dict (name, underlay, nodes, links).
    """
    with _TOPO_FILE.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "nodes" not in data:
        raise HTTPException(500, f"malformed topology file: {_TOPO_FILE}")
    return data


def _inventory_devices() -> list[dict[str, Any]]:
    """All inventory rows (502 with cause on failure)."""
    try:
        r = httpx.get(f"{INVENTORY_URL}/devices", timeout=15)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"inventory unreachable: {exc}") from exc
    if r.status_code != 200:
        raise HTTPException(502, f"inventory error {r.status_code}")
    return r.json()


def _node_payload(node: dict[str, Any]) -> str:
    """Render one node's baseline config (SET format, merge-load).

    SET format on purpose: ``delete <path>`` lines are only valid in
    set-format loads (text-format rejects them with a syntax error), and
    set lines have no brace-balancing hazard at all. Cleanup deletes
    come first so stale entries vanish before the plan's values land.
    """
    lines: list[str] = [f"delete {c}" for c in node.get("cleanup") or []]
    lb = str(node["loopback"])
    lines.append(f"set interfaces lo0 unit 0 family inet address {lb}/32")
    lines.append(f"set routing-options autonomous-system {node['asn']}")
    return "\n".join(lines)


class ApplyNodeResult(BaseModel):
    """Per-node outcome of POST /topology/apply."""

    node: str
    ok: bool
    diff_lines: int = 0
    error: str = ""


class ApplyResult(BaseModel):
    """Aggregate outcome of one apply run."""

    topology: str
    applied: int
    failed: int
    nodes: list[ApplyNodeResult]


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — smoke-test 200 target."""
    return {"service": "restor8-topology", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target."""
    return {"status": "ok"}


@app.get("/topology")
def topology() -> dict[str, Any]:
    """The plan itself: name, underlay, nodes, links."""
    return _load()


@app.get("/topology/reconcile")
def reconcile() -> dict[str, Any]:
    """Plan vs inventory: which planned nodes are registered, and which
    inventory devices the plan doesn't cover (auto-register candidates).

    This is Phase 4's checkpoint surface: every planned node resolving to
    a registered, reachable mgmt address.
    """
    topo = _load()
    planned = {n["name"]: n for n in topo["nodes"]}
    registered = {d["name"]: d for d in _inventory_devices()}
    return {
        "topology": topo["name"],
        "underlay": topo.get("underlay"),
        "planned_nodes": len(planned),
        "missing_from_inventory": sorted(set(planned) - set(registered)),
        "unplanned_devices": sorted(set(registered) - set(planned)),
        "ready": not (set(planned) - set(registered)),
    }


@app.post("/topology/apply", response_model=ApplyResult)
def apply() -> ApplyResult:
    """Push every planned node's baseline through connector.

    Idempotent by construction: the payload is derived only from the
    plan (+ cleanup), so re-running converges each node to the plan.
    Sequential on purpose — lab-scale (10 nodes), and the event stream
    in connector's logs reads in apply order.
    """
    topo = _load()
    registered = {d["name"]: d for d in _inventory_devices()}
    results: list[ApplyNodeResult] = []

    for node in topo["nodes"]:
        name = node["name"]
        dev = registered.get(name)
        if dev is None:
            results.append(
                ApplyNodeResult(node=name, ok=False, error="not in inventory — run /topology/reconcile")
            )
            continue
        payload = _node_payload(node)
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
                    "comment": f"restor8-topology: baseline {topo['name']}",
                    "confirm_now": True,
                },
                timeout=300,
            )
        except httpx.HTTPError as exc:
            results.append(ApplyNodeResult(node=name, ok=False, error=str(exc)))
            continue
        if r.status_code != 200:
            detail = r.json().get("detail", {})
            results.append(
                ApplyNodeResult(node=name, ok=False, error=str(detail.get("message", r.status_code)))
            )
            continue
        diff = r.json().get("diff", "")
        changed = sum(
            1
            for line in diff.splitlines()
            if line[:1] in "+-" and line[:3] not in ("+++", "---")
        )
        results.append(ApplyNodeResult(node=name, ok=True, diff_lines=changed))

    return ApplyResult(
        topology=str(topo["name"]),
        applied=sum(1 for x in results if x.ok),
        failed=sum(1 for x in results if not x.ok),
        nodes=results,
    )
