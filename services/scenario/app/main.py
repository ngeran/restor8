"""scenario — the protocol test engine (Phase 5).

Why this service exists: "run bgp-full-mesh against the lab and tell me if
it converged" is restor8's fourth job. A run is a state machine executed
against real devices — render from the scenario definition, push through
connector (confirmed-commit), poll convergence, validate with JSNAPy,
store the outcome — and every step of it feeds the event stream the
gateway will fan out (Phase 6).

Definitions are CODE (YAML + Jinja2 + JSNAPy testfiles in this repo,
baked into the image); outcomes are DATA (SQLite on a PVC).

Choreography (scenario touches no devices):
    topology   GET /topology            roles, ASNs, node list
    inventory  GET /devices             addresses, auth_refs
    connector  POST /snapshot           eth0 pod-IP discovery, bgp summary
    connector  POST /push               rendered config (set-format)
    core       jsnapy_runner.compare    convergence validation
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from db import RunDB
from restor8_core.jsnapy_runner import compare

INVENTORY_URL = "http://restor8-inventory:8080"
CONNECTOR_URL = "http://restor8-connector:8080"
TOPOLOGY_URL = "http://restor8-topology:8080"

_APP = Path(__file__).parent
_ENV = Environment(
    loader=FileSystemLoader(str(_APP / "templates")),
    undefined=StrictUndefined,  # a missing var must fail loudly, not render ""
    trim_blocks=True,
    lstrip_blocks=True,
)

log = logging.getLogger("restor8.scenario")
logging.basicConfig(level=logging.INFO, format="%(message)s")

db = RunDB()

app = FastAPI(
    title="restor8 scenario",
    description="Protocol scenario engine: render → push → converge → validate → store.",
    version="0.1.0",
)


# ── small upstream helpers ─────────────────────────────────────────────


def _get(url: str) -> httpx.Response:
    """GET with 502 mapping (upstream is cluster-internal)."""
    try:
        r = httpx.get(url, timeout=30)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unreachable ({url}): {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(502, f"upstream {r.status_code} from {url}")
    return r


def _snapshot(dev: dict[str, Any], rpc: str, args: dict[str, str] | None = None) -> str:
    """One operational snapshot XML via connector for an inventory row."""
    try:
        r = httpx.post(
            f"{CONNECTOR_URL}/snapshot",
            json={
                "host": dev["mgmt_ip"],
                "port": dev["port"],
                "auth_ref": dev["auth_ref"],
                "rpc": rpc,
                "args": args or {},
            },
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"connector unreachable: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(str(r.json().get("detail", {}).get("message", r.status_code)))
    return r.json()["xml"]


# ── peer-address discovery ─────────────────────────────────────────────
#
# The flat-underlay reality (learned the hard way, run 2): each
# clabernetes launcher pod runs its OWN private bridge — every cRPD's
# eth0 is 172.20.20.2, pod-local, NOT cluster-routable. The cluster
# address of a node is its LAUNCHER POD IP (10.42.x.y), which is what
# the per-node Services actually select — and what a BGP session's
# source address becomes (the launcher bridge masquerades outgoing
# traffic to the pod IP). So the mesh peers on launcher pod IPs,
# discovered live from the k8s API (they move if a launcher restarts).

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _launcher_pod_ip(node_service: str) -> str:
    """Pod IP of a node's clabernetes launcher, from the k8s API.

    Args:
        node_service: the inventory containerlab_node name (p1, p2, …) —
            launcher pods are named `<node>-<hash>` in ns `topology`.

    Returns:
        The launcher pod's IP (10.42.x.y).

    Raises:
        RuntimeError: API failure or no matching pod.
    """
    token = (_SA / "token").read_text() if (_SA / "token").exists() else ""
    try:
        r = httpx.get(
            "https://kubernetes.default.svc/api/v1/namespaces/topology/pods",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            verify=str(_SA / "ca.crt") if (_SA / "ca.crt").exists() else False,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"k8s API unreachable: {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"k8s API {r.status_code}: {r.text[:200]}")
    for pod in r.json().get("items", []):
        name = pod["metadata"]["name"]
        # Case-insensitive: inventory names (P-1) vs pod prefixes (p1-…)
        if name.split("-")[0].lower() == node_service.lower() and pod.get("status", {}).get("podIP"):
            return str(pod["status"]["podIP"])
    raise RuntimeError(f"no launcher pod found for '{node_service}' in ns topology")


# ── convergence polling ────────────────────────────────────────────────


def _bgp_state(xml: str) -> tuple[int, int]:
    """(established, total) peer counts from a bgp-summary snapshot."""
    total = xml.count("<bgp-peer")
    established = xml.count("<peer-state>Established</peer-state>")
    return established, total


# ── the run state machine ──────────────────────────────────────────────


def _execute(run_id: int, definition: dict[str, Any]) -> None:
    """Run one scenario to completion; every phase recorded in detail."""
    detail: dict[str, Any] = {"phases": [], "nodes": {}}
    status = "failed"

    def phase(name: str, **info: Any) -> None:
        detail["phases"].append({"phase": name, **info})
        log.info(json.dumps({"run": run_id, "phase": name, **info}))

    try:
        plan = _get(f"{TOPOLOGY_URL}/topology").json()
        devices = {d["name"]: d for d in _get(f"{INVENTORY_URL}/devices").json()}
        roles = set(definition.get("node_roles", []))
        planned = [
            n for n in plan["nodes"] if not roles or n.get("role") in roles
        ]
        phase(
            "plan",
            nodes=len(planned),
            underlay=plan.get("underlay"),
            missing=sorted(n["name"] for n in planned if n["name"] not in devices),
        )

        # 1. discover peering addresses (launcher pod IPs, see discovery note)
        mesh: dict[str, dict[str, Any]] = {}
        for n in planned:
            dev = devices.get(n["name"])
            if dev is None:
                raise RuntimeError(f"{n['name']} not in inventory")
            mesh[n["name"]] = {
                "dev": dev,
                "asn": n["asn"],
                "ip": _launcher_pod_ip(str(dev.get("containerlab_node") or n["name"])),
            }
        phase("discovered", peers={k: v["ip"] for k, v in mesh.items()})

        # 2. pre-change snapshot (spec §4: pre AND post for every run)
        pres = {
            name: _snapshot(m["dev"], "get-bgp-summary-information")
            for name, m in mesh.items()
        }
        phase("pre-snapshot", taken=len(pres))

        # 3. render + push per node
        template = _ENV.get_template(str(definition["template"]).split("/")[-1])
        for name, m in mesh.items():
            peers = [
                {"ip": other["ip"], "asn": other["asn"]}
                for oname, other in mesh.items()
                if oname != name
            ]
            payload = template.render(peers=peers, **definition.get("vars", {}))
            r = httpx.post(
                f"{CONNECTOR_URL}/push",
                json={
                    "host": m["dev"]["mgmt_ip"],
                    "port": m["dev"]["port"],
                    "auth_ref": m["dev"]["auth_ref"],
                    "payload": payload,
                    "fmt": "set",
                    "mode": "merge",
                    "comment": f"restor8-scenario: {definition['name']}",
                    "confirm_now": True,
                },
                timeout=300,
            )
            ok = r.status_code == 200
            detail["nodes"][name] = {"pushed": ok}
            if not ok:
                raise RuntimeError(
                    f"push failed on {name}: {r.json().get('detail', {}).get('message', r.status_code)}"
                )
        phase("pushed", nodes=len(mesh))

        # 4. poll convergence
        timeout = int(definition.get("convergence_timeout", 120))
        interval = int(definition.get("poll_interval", 5))
        min_peers = int(definition.get("min_peers", len(mesh) - 1))
        converged = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {
                name: _bgp_state(_snapshot(m["dev"], "get-bgp-summary-information"))
                for name, m in mesh.items()
            }
            detail["nodes"] = {
                name: {**detail["nodes"][name], "established": e, "peers": t}
                for name, (e, t) in states.items()
            }
            if all(
                e >= min_peers and t >= min_peers for e, t in states.values()
            ):
                converged = True
                break
            time.sleep(interval)
        phase(
            "converged" if converged else "convergence-timeout",
            timeout=timeout,
            states={k: f"{e}/{t}" for k, (e, t) in states.items()},
        )
        if not converged:
            raise RuntimeError("convergence timeout — see per-node states")

        # 5. JSNAPy post-validation (pre vs post, file-based)
        testdef = yaml.safe_load(
            (_APP / definition["jsnapy_test"]).read_text()
        )
        jsnapy_results = {}
        all_passed = True
        for name, m in mesh.items():
            post = _snapshot(m["dev"], "get-bgp-summary-information")
            res = compare(testdef, pres[name], post)
            jsnapy_results[name] = {"passed": res.passed, "results": res.results}
            all_passed = all_passed and res.passed
        detail["jsnapy"] = jsnapy_results
        phase("jsnapy", passed=all_passed)
        if not all_passed:
            raise RuntimeError("JSNAPy validation failed")

        status = "passed"
        phase("done", status=status)
    except Exception as exc:  # noqa: BLE001 — run isolation: record & finish
        detail["error"] = str(exc)
        log.exception("run %s failed", run_id)
    finally:
        db.finish(run_id, status, json.dumps(detail))


# ── API ────────────────────────────────────────────────────────────────


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — smoke-test 200 target."""
    return {"service": "restor8-scenario", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target."""
    return {"status": "ok"}


@app.get("/scenarios")
def scenarios() -> list[dict[str, Any]]:
    """List available scenario definitions (from the repo, not the DB)."""
    out = []
    for f in sorted((_APP / "scenarios").glob("*.yml")):
        d = yaml.safe_load(f.read_text())
        out.append(
            {
                "name": d["name"],
                "description": d.get("description", ""),
                "convergence_timeout": d.get("convergence_timeout", 120),
            }
        )
    return out


@app.post("/scenario/{name}/run")
def run_scenario(name: str) -> dict[str, Any]:
    """Start a run — returns immediately with the run ID (poll the GET)."""
    f = _APP / "scenarios" / f"{name}.yml"
    if not f.exists():
        raise HTTPException(404, f"unknown scenario '{name}' (see /scenarios)")
    definition = yaml.safe_load(f.read_text())
    run_id = db.start(name)
    threading.Thread(
        target=_execute, args=(run_id, definition), daemon=True
    ).start()
    return {"run": run_id, "scenario": name, "status": "running"}


@app.get("/scenario/run/{run_id}")
def run_status(run_id: int) -> dict[str, Any]:
    """One run: status + detail (phases, per-node, jsnapy)."""
    row = db.get(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} not found")
    try:
        row["detail"] = json.loads(row["detail"])
    except ValueError:
        pass
    return row


@app.get("/scenario/{name}/runs")
def scenario_runs(name: str) -> list[dict[str, Any]]:
    """Run history for one scenario."""
    return db.list_runs(name)
