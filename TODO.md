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
- [ ] **Checkpoint:** break p3's BGP intentionally, restore from last good
      backup, JSNAPy green + BGP reconverges. **BLOCKED on the data-plane
      question:** the lab (clabernetes, ns `topology`) has no inter-node
      links configured — eth1–5 exist unconnected (p2↔p3 eBGP over
      10.99.23.0/30 stayed `Active`); the `-vx` services look like prepared
      VXLAN endpoints (ports 4799/14789) with nothing wired. Baseline
      eBGP/lo0 configs pushed to p2/p3 via /push are staged and committed
      on the devices.

## Phase 4 — Topology awareness
- [ ] Scaffold `services/topology`: parse Containerlab `.clab.yml`, map node
      names → inventory (auto-register unknown nodes as candidates).
- [ ] **Checkpoint:** point at an existing 10-node MPLS topology; every node
      resolves to a reachable mgmt IP.

## Phase 5 — Scenario engine
- [ ] Scaffold `services/scenario`: scenario = YAML (protocol, target nodes,
      Jinja2 template vars, JSNAPy test file, convergence timeout).
- [ ] ONE scenario end-to-end (`bgp-full-mesh`) before generalizing to
      ISIS/OSPF/LDP-TE/MPLS/L3VPN/TWAMP.
- [ ] Run flow: render templates → connector push (confirmed commit) →
      poll convergence → JSNAPy check → store result.
- [ ] **Checkpoint:** run `bgp-full-mesh` via curl start-to-finish; pass/fail
      matches reality (verify with `show bgp summary` on devices).

## Phase 6 — Real-time feedback (gateway)
- [ ] Scaffold `services/gateway`: WS fan-out of connector progress events +
      scenario-run progress, keyed by run/session ID.
- [ ] **Checkpoint:** raw WS client (`websocat`), trigger a scenario via REST,
      watch live per-step events arrive before the run completes.

## Phase 7 — Frontend
- [ ] Scaffold `frontend/` (React + Tailwind v4, `nix flake init -t ~/.omni-nix#react`).
- [ ] Screens in order: Dashboard → Devices → Configurations (git diff +
      history + revert, against real Phase 2/3 API) → Topology (draggable
      canvas, live status via gateway WS). Design tokens per §3.
- [ ] **Checkpoint:** each screen wired to its real backend before the next;
      no screen ships against fixtures only.

## Phase 8 — Deploy + harden
- [ ] `manifests/` per service in ns `restor8`, Ingress `restor8.home`
      (existing Traefik pattern).
- [ ] `just doctor` / `just check` clean across every service.
- [ ] Observability: structured JSON logs from each service into the existing
      ARGO/PULSE stack (Prometheus/Grafana/Loki) — same scrape pattern as the
      rest of the cluster.
