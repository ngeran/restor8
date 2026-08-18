---
name: restor8
description: >
  Build and extend restor8, Nikos's Juniper network automation platform
  (config/backup/restore/protocol-test scenarios for cRPD, vJunos, MX,
  ACX) running on the home-lab k3s cluster alongside Containerlab
  topologies. Use this skill for any work inside the restor8 repo, or
  when adding a new microservice, PyEZ/JSNAPy integration, protocol test
  scenario (BGP, ISIS, OSPF, LDP-TE, MPLS, L3VPN, TWAMP), or frontend
  screen to it. Trigger on: restor8, PyEZ, JSNAPy, connector service,
  scenario engine, config backup/restore, or any mention of Junos
  device automation in this repo.
---

# restor8 — Juniper network automation platform

restor8 configures, backs up, restores, and protocol-tests Juniper lab
gear (cRPD, vJunos-router, MX, ACX) from a k3s-hosted control plane
against Containerlab topologies. It's built as a set of small FastAPI
services on top of Nikos's standard `project-pipeline` skill — read that
skill first for the Nix/uv2nix/k3s mechanics; this skill covers what's
specific to restor8.

## Architecture recap

```
libs/restor8_core/     shared package: PyEZ wrapper, JSNAPy runner, pydantic models, event schema
services/inventory/    device CRUD (SQLite)
services/connector/    owns ALL live PyEZ/NETCONF sessions — everything else calls this over HTTP
services/backup/       pulls config via connector, commits to a Git repo (one dir per device)
services/restore/      diffs a backup commit vs. running config, pushes it back with pre/post JSNAPy
services/scenario/     protocol test engine — YAML scenario defs + Jinja2 templates + JSNAPy checks
services/topology/     parses Containerlab .clab.yml, maps nodes -> inventory
services/gateway/      BFF: REST aggregation + WebSocket fan-out of connector/scenario progress events
frontend/              React + Tailwind v4, ConfigKnit design system (see below)
```

**Hard rule:** only `connector` imports PyEZ / opens NETCONF sessions.
Every other service reaches devices through `connector`'s HTTP API. This
is what keeps the live-progress event stream coherent for the UI — don't
let a new service "just quickly" open its own `jnpr.junos.Device` because
it's convenient; route it through connector even if that means a new
connector endpoint.

## PyEZ / JSNAPy patterns

- Config pushes: always `lock() → load() → diff() → commit(confirm=2)`.
  Never a bare `commit()` against lab gear that's mid-iteration on
  protocol config — confirmed commit is the safety net.
- Every connector operation emits progress events (`connecting`,
  `locking`, `loading_config`, `diff_ready`, `committing`,
  `commit_confirmed`, `error`, ...) via the callback defined in
  `restor8_core/events.py`. New connector operations must emit events at
  each real state transition, not just at start/end.
- JSNAPy test files live under `services/scenario/jsnapy_tests/<protocol>/`,
  one YAML per protocol family. Snapshot pre AND post for every scenario
  run — never validate against a single post-only snapshot.
- Map every PyEZ exception (`ConnectError`, `LockError`, `CommitError`,
  `RpcTimeoutError`, ...) to a typed error in `restor8_core/models.py`.
  Never swallow the underlying exception message.
- Comment every RPC call with the Junos CLI command it corresponds to.

## Scenario definitions

A scenario is a YAML file, not a database row — it's reviewed like code.

```yaml
# services/scenario/scenarios/bgp-full-mesh.yml
name: bgp-full-mesh
protocol: bgp
description: Full-mesh iBGP between all core nodes in the topology
targets: [core-01, core-02, core-03]      # containerlab node names
template: templates/bgp/full_mesh.j2
template_vars:
  as_number: 65001
jsnapy_test: jsnapy_tests/bgp/full_mesh.yml
convergence_timeout_s: 60
```

New protocol scenarios (ISIS, OSPF, LDP-TE, MPLS, L3VPN, TWAMP) follow
this same shape — add the Jinja2 template under `templates/<protocol>/`,
the JSNAPy check under `jsnapy_tests/<protocol>/`, and the scenario YAML.
Don't generalize the engine further than this until a second protocol
actually needs something the shape doesn't support.

## Design system (frontend)

**Source of truth: `frontend/app/src/index.css` — see [DESIGN.md](../../../DESIGN.md) for the full palette and rationale.** Summary:

| Token | Value |
|---|---|
| Background | `#000000` (OLED true black; panels black, cards `#050505`) |
| Border | `#16161c` |
| Primary accent | `#59c2ff` (blue) |
| Secondary accent | `#ffb454` (orange) |
| Success / Warning / Error | `#7ce38b` / `#ffd173` / `#ff6b6b` |
| Fonts | JetBrains Mono (data) / Inter (prose) |
| Radius | `0.25rem`, sharp |

Accent load is spread across channels (blue/orange/yellow/green) on true
black — OLED burn-in friendly; glow marks live/active state only.

## Build order

Follow the phased checkpoints in `RESTOR8_CLAUDE_CODE_PROMPT.md` /
`TODO.md` at the repo root. Each phase has a real-device checkpoint that
must pass before the next phase starts — this is a hardware-integration
project, so the connector/PyEZ boundary gets proven early, not last.

## When adding a new service

Follow `project-pipeline`'s Python scaffold exactly
(`variant = "fastapi"`, uv2nix, non-root image) — then:
1. Add it to `manifests/` under the `restor8` namespace.
2. If it needs device access, it calls `connector`; it does not import PyEZ.
3. Give it a `just test` smoke check before wiring it into `gateway`.
