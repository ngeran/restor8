# restor8 — build ledger

Mirrors §6 of the project spec. **Do not start a phase until the previous
one's checkpoint passes against a real device.** Update this file as each
item lands. The wire (PyEZ/NETCONF) is where the bugs live — prove it
early, prove it often.

## Locked decisions (asked & answered 2026-08-16 — don't re-ask)

| Decision | Choice |
|---|---|
| Device auth | **Shared lab credential** — one username+password in k8s Secret `restor8/lab-auth`, injected as `LAB_USER`/`LAB_PASSWORD`. `inventory.auth_ref` defaults to it; per-device Secret can override later without schema change. |
| Restore gating | **Manual approve, auto-rollback** — a human confirms the push in the UI; once pushed, confirmed-commit + JSNAPy post-check roll back automatically on failure. No unattended restores. |
| Vendor scope | **Junos-only** (cRPD, vJunos-router, MX, ACX). PyEZ-native connector, no driver abstraction. Multi-vendor would mean NAPALM later, connector rewrite only. |
| Repo layout | **Single root flake + uv workspace** (smallest pipeline deviation). One `flake.nix`/`uv.lock`/`justfile` at root; per-service images (`nix build .#<service>`), devshells, and `manifests/<service>/` in ns `restor8`. Chosen because a per-service flake cannot reference `../../libs` — flake evaluation is rooted at the flake dir, so per-service scaffolds would force vendoring `restor8_core` seven ways. |

## Phase 0 — Foundation + prove the wire works
- [x] Scaffold `libs/restor8_core` (uv workspace member, not a service).
- [x] Scaffold `services/connector` (root flake, `variant = "fastapi"` equivalent).
- [x] Implement `JunosConnection` wrapper with event callbacks (§4).
- [x] One endpoint: `POST /connect` `{host, user, auth}` → NETCONF session, `facts`, close.
- [x] **Checkpoint:** `just run` locally, `curl` against a real vJunos/cRPD in the
      containerlab topology, confirm real facts (model, version, hostname) + the
      event sequence in logs. Not a mock. Do not proceed until this works.
      **PASSED 2026-08-16** — cRPD `P-1` (CRPD 25.4R1-S2.3) via localhost:31001:
      facts returned through `POST /connect`, full `resolving → connecting →
      authenticating → connected → closed` sequence in the service log. Two bugs
      found & fixed by the checkpoint: facts serialization (PyEZ `version_info`
      object → JSON-safe coercion in `models._jsonable`) and event logging
      (INFO events had no handler under stock uvicorn → `logging.basicConfig`).
      **In-cluster re-verified same day:** deployed to ns `restor8` (Secret
      `lab-auth` → env creds), port-forwarded, same facts via node IP
      `10.0.0.29:31001` with NO creds in the request, full event stream in
      `kubectl logs`. Deploy runbook recorded in CLAUDE.md.

## Phase 1 — Inventory
- [x] Scaffold `services/inventory`, SQLite schema: devices
      (name, mgmt_ip, platform, port, auth_ref, containerlab_node, created_at).
- [x] CRUD API + `just test` smoke (build image → run → curl → 200).
      SQLite on a PVC (`/data`, local-path, 512Mi) — proven to survive pod
      deletion. Default DB path `/tmp/inventory.db` (read-only-rootfs safe).
- [x] **Checkpoint:** registered ALL 10 lab cRPDs via the API, listed back.
      **DISCOVERY:** the lab already runs *inside k3s* (ns `topology`) — each
      node has a ClusterIP svc exposing NETCONF :830 (+ `-host` NodePorts
      31xxx/SSH and 32xxx/NETCONF, + `-vx` VXLAN data-plane svcs). Inventory
      stores cluster-DNS addresses (`p1.topology.svc.cluster.local:830`) —
      verified working from the connector pod for P-1 (facts returned).
      P-1 auth: admin/manolis1 (the `lab-auth` Secret). **Open:** creds for
      p2-p4/pe/rr/ce differ (ConnectAuthError on p2) — verify each node's
      real hostname via connector facts and PATCH when known; names beyond
      P-1 are assumed from the service-name convention.
      **Phase 4 impact:** topology awareness should likely WATCH the
      `topology` namespace (kubectl/ownerReferences) instead of parsing
      containerlab YAML — decide there.

## Phase 2 — Backup
- [x] Scaffold `services/backup` (calls connector over HTTP, never imports PyEZ).
- [x] `POST /backup/{device_id}` → connector pulls config → git commit
      (`backup: <device> @ <timestamp>`) into the device's directory (PVC).
      Idempotent: unchanged config → no commit. Repo on 1Gi PVC (`/data/repo`),
      git binary baked into the image (the one flagged image deviation).
- [x] `GET /backup/{device_id}/history` → `git log` for that path.
- [x] **Checkpoint PASSED 2026-08-16** — p3: backup → commit `d1d78b3132d5`;
      re-backup → `changed=false` (no no-op commits); pushed lo0 description
      via connector `/push` (confirmed-commit pipeline, diff returned);
      backup again → commit `60ae0c773bde`; history shows both.
      Bonus landed for Phase 2/3: connector `/config` (pull) + `/push`
      endpoints, per-device `auth_ref` credential resolution (Secret
      `restor8/lab-auth-root` = root/clab123 for the 9 clab nodes; P-1 keeps
      admin@lab-auth) — all 10 nodes verified with real facts, inventory
      names corrected to real hostnames (lowercase p2…ce2; only P-1 is
      uppercase). Ingress added: connector/inventory at
      `*.restor8.home` via Traefik (needs /etc/hosts → 10.0.0.29).

## Phase 3 — Restore
- [x] Scaffold `services/restore`.
- [x] `GET /restore/{device_id}/diff/{commit_sha}` → unified diff commit vs
      running config (via connector, no commit yet).
- [x] `POST /restore/{device_id}/{commit_sha}?approve=true` → confirmed-commit
      push on a HELD connector session (confirming commit must share the
      NETCONF session — connector now holds sessions for the window and
      exposes /session/{id}/confirm|rollback), post-check, auto-confirm on
      pass / auto-rollback on fail. Manual-approve gate per locked decision.
- [x] Validation: JSNAPy when a `testdef` is supplied (file-based compare in
      restor8_core.jsnapy_runner — verified live against real bgp-summary
      XML, PASS/FAIL discriminated), config-match equality otherwise.
      Connector gained `/snapshot` (RPC-by-name → XML). Backup gained
      `GET /backup/{id}/config/{sha}`.
      JSNAPy quirks (JSNAPY_HOME, two-file config, its %-bug at jsnapy.py:795,
      stock logging.yml killing our loggers) are encapsulated in the runner.
- [x] **Checkpoint PASSED 2026-08-16** — broke p3 (deleted AS + lo0 baseline
      via set-format /push), restore without approval → 403 (gate works),
      `POST /restore/7/latest?approve=true` → override push on a held
      session → config-match validation green → confirmed commit →
      `restored: true`. JSNAPy variant is exercised by Phase 5 (needs live
      BGP; runner itself is already live-verified). Bugs found by the
      checkpoint: stale backup image (config@sha undeployed), restore's
      HTTP helper GETing POST-only session routes, finally-unlock masking
      real errors (cRPD closes the candidate DB after confirming commit —
      unlock now best-effort), PyEZ rollback kwarg (`rb_id=`), and Junos
      text-parser quirks (needs spaces between `}}`; `delete` lines only
      valid in set-format loads → topology payloads are set-format now).

## Phase 4 — Topology awareness
- [x] Scaffold `services/topology` — RESHAPED (see Phase 1 discovery): the
      lab is clabernetes-in-k8s with all nodes already in inventory, so
      instead of parsing containerlab YAML the service owns a declarative
      plan checked into the repo (`services/topology/app/topologies/
      mpls-core.yml`: nodes with role/asn/loopback/cleanup, intended links,
      `underlay: flat-podnet` until a real fabric is wired).
- [x] Endpoints: `GET /topology` (plan), `GET /topology/reconcile`
      (plan ↔ inventory diff), `POST /topology/apply` (per-node baseline
      push via connector — set-format payload: cleanup deletes + lo0 + AS;
      idempotent).
- [x] **Checkpoint PASSED 2026-08-16** — reconcile: 10/10 planned nodes
      registered, ready. apply: **all 10 lab devices configured through
      the app** (10/10 ok; p2/p3 also cleaned of hand-staged Phase 2/3
      recon config). All 10 backed up into Git history immediately after
      (fresh app-era baseline commits per device).

## Phase 5 — Scenario engine
- [x] Scaffold `services/scenario`: scenario = YAML + Jinja2 template +
      JSNAPy testfile, checked into the image; run history in SQLite on a
      PVC. Background-thread runs; `POST /scenario/{name}/run` → poll
      `GET /scenario/run/{id}`.
- [x] ONE scenario end-to-end (`bgp-full-mesh`) before generalizing.
- [x] Run flow: topology plan + inventory lookup → peer-address discovery
      (k8s API, RBAC'd SA reading launcher pod IPs in ns topology) →
      Jinja2 render → connector push (set-format, confirmed commit) →
      convergence polling (established/total per node) → JSNAPy pre/post
      compare → result stored.
- [x] **Checkpoint PASSED 2026-08-16 (run 8)** — the missing piece was the
      HOST MPLS KERNEL MODULES (hive's own README warned: without them
      cRPD's eth1+ never register; `/proc/sys/net/mpls` was empty).
      After `sudo modprobe mpls_router mpls_iptunnel` + launcher restarts,
      the 17 clabernetes links came alive. restor8's plan was rewritten to
      the REAL fabric (hive `04-routing/manifests/02-topology.yaml`),
      underlay applied via the app (per-link /30s + loopbacks + ASNs),
      and `bgp-fabric` went green: converged (P 5/5, PE/RR 3/3, CE 1/1),
      JSNAPy passed ×10, independently verified on devices (p2: 5 peers,
      0 down, 122 prefixes; P-1 root password now manolis1, durably in
      hive's p1.conf). Lessons burned in: cRPD terse bgp-summary has NO
      per-peer state element — use header peer-count/down-peer-count;
      newlines live INSIDE cRPD's XML tags everywhere; jsnapy writes to
      ~/.jsnapy → HOME=/tmp in the deployment; launcher restarts reset
      nodes to ConfigMap startup-config (config must be re-appliable —
      it is, idempotently).

## Phase 6 — Real-time feedback (gateway)
- [x] Scaffold `services/gateway`: `POST /internal/events` ingest → in-memory
      bus (bounded queues, drop-oldest — a slow browser never back-pressures
      a device op) → `WS /ws` fan-out with `?session/&device/&run` filters;
      REST aggregation (`/api/*`: devices, topology, backups, diff,
      scenarios, runs + run-start action).
- [x] Event producers: restor8-core `relay_sink` (log + best-effort POST on
      a worker thread; gateway down ≠ device op failure) wired into all
      connector endpoints; scenario relays phase records the same way.
- [x] **Checkpoint PASSED 2026-08-16** — `websocat -n` (devShell client;
      note: websocat exits on stdin-EOF in background — `-n` keeps it up)
      subscribed to `/ws`; triggered `bgp-fabric` via gateway REST; at
      t+40s with the run still `running`, the subscriber had received
      3 scenario phases (plan/mesh/pre-snapshot) + 145 device events.
      Gateway ingested 300+ events across the run.

## Phase 7 — Frontend
- [x] Scaffold `frontend/` (omni-nix react template — own flake/justfile,
      Vite + React + TypeScript + Tailwind v4; `just relock` for npmDepsHash).
- [x] Screens: Dashboard (summary cards, run list + start button, live
      event ticker over WS), Devices (inventory table with plan roles),
      Configurations (device → commit history → unified diff, red/green
      blocks + line numbers, "in sync" state), Topology (draggable SVG,
      ring layout by role, links + nodes glow accent on live WS events).
      Design tokens per §3 (bg/card/border/accent palette, mono/sans
      stacks, 0.25rem radius, glow-as-signal).
- [x] **Checkpoint PASSED 2026-08-16** — same-origin wiring via Ingress:
      `restor8.home/` → SPA, `/api/*` → gateway, `/ws` → gateway (101
      upgrade verified). `/api/devices` returns the live registry through
      the SPA origin; dev parity via vite proxy (:5173 → gateway
      port-forward). Run 14 started through the gateway API exactly as
      the UI does. Restore's approve/revert buttons: Phase 8 polish.
      Gotchas banked: nested frontend flake needs `git add -A` like
      everything else; template manifests ship react-app names + default
      ns — renamed to restor8-frontend/ns restor8; Traefik needs a few
      seconds to reconcile new path rules.

## Phase 8 — Deploy + harden
- [x] `manifests/` per service in ns `restor8`, Ingress `restor8.home`
      (existing Traefik pattern). **Landed in flight** — all 9 deployments
      (8 services + frontend) live; `manifests/ingress.yaml` routes
      `restor8.home` → SPA + `/api`/`/ws` → gateway, with
      connector/inventory subdomains for direct wire-testing.
- [x] `just doctor` / `just check` clean across every service.
      **2026-08-20** — gate sweep: gateway E402 (hoisted `re`/`time`),
      `JunosConnection.rpc(**kwargs)` widened `str` → `str | bool` (its own
      docstring always documented `terse=True`), `_parse_interfaces` typing.
      ruff strict + mypy green across core + all 8 services; doctor ready.
- [ ] Observability: structured JSON logs from each service into the existing
      ARGO/PULSE stack (Prometheus/Grafana/Loki) — same scrape pattern as the
      rest of the cluster.


## Phase A — Topology from live configuration (2026-08-22)
- [x] A1 `GET /api/topology/discover`: links inferred by /30 pairing over the
      live interface cache; dangling detection; nodes with reachability.
      **Verified:** 10/10 reachable, 17 planned links discovered PLUS 6
      unplanned 10.255.x links (real config the plan doesn't know) —
      discovery reports true state. Dangling proven during mid-repair.
- [x] A2 Topology screen: `◉ live (discovered)` is the DEFAULT view (30s
      poll); `▤ plan` overlay toggle; dangling links as dashed-yellow stubs;
      unreachable nodes red-dashed. Full-bleed canvas, persisted layout,
      live hovers unchanged. **Verified** via deployed bundle + SPA origin
      (23 links, 10 nodes up).
- [ ] A3 Lab snapshots (Phase B of the validated plan) — next.
