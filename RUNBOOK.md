# restor8 Runbook — using, changing, and fixing the lab

The operating manual for the restor8 platform and its lab. The lab has two
repos that must stay in agreement:

| Repo | Owns |
|---|---|
| `~/github/ngeran/restor8` | the app: services, topology PLAN (addressing/ASNs), scenarios, UI |
| `~/github/ngeran/hive` (`04-routing/`) | the FABRIC: clabernetes Topology CR (nodes + physical links), cRPD startup configs |

**Rule of thumb: hive decides what EXISTS (nodes, links); restor8 decides
what it MEANS (addresses, ASNs, protocols).** Changing one usually means
changing the other.

---

## 1. Open the app

The frontend's nginx proxies `/api` + `/ws` to the gateway, so **any
address that reaches the frontend pod serves the complete app**.

```bash
# via the frontend POD IP (changes on every redeploy):
kubectl -n restor8 get pod -l app=restor8-frontend -o jsonpath='{.items[0].status.podIP}'
# → browse  http://<podIP>:8080

# via the frontend ClusterIP (stable across redeploys; host-only):
kubectl -n restor8 get svc restor8-frontend -o jsonpath='{.spec.clusterIP}'
# → browse  http://<clusterIP>/        (service port is 80)
```

Screens: **Dashboard** (summary, run button, live event ticker) ·
**Devices** (inventory + roles) · **Configurations** (per-device commit
history + diff vs running) · **Topology** (draggable fabric map, nodes
glow while events name them).

Hot-reload development instead of the deployed UI:

```bash
kubectl -n restor8 port-forward svc/restor8-gateway 18086:8080   # terminal 1
cd frontend && nix develop -c 'npm run dev'                       # terminal 2 → http://localhost:5173
```

(If `18086` is "already in use": a stale forwarder is squatting —
`pgrep -af "kubectl.*port-forward"` then kill it.)

---

## 2. The four jobs (UI action → API equivalent)

### Run a protocol scenario
Dashboard → `▶ run bgp-fabric`, or:

```bash
GATE=http://$(kubectl -n restor8 get svc restor8-gateway -o jsonpath='{.spec.clusterIP}')
curl -X POST $GATE/api/scenarios/bgp-fabric/run      # → {"run": N, ...}
curl $GATE/api/runs/N                                 # poll: running → passed/failed
```

Definition files (they are code, baked into the image — edit + deploy to
change): `services/scenario/app/scenarios/*.yml` + `templates/*.j2` +
`jsnapy_tests/<protocol>/*.yml`.

### Watch the live feed
The Dashboard ticker (or any WS client):

```bash
nix develop -c bash -c "websocat -n ws://$GATE/ws"                 # everything
#                                                    ?device=p2   one device
#                                                    ?run=14      one scenario run
```

### Back up a device (config → Git history)
```bash
BACKUP=http://$(kubectl -n restor8 get svc restor8-backup -o jsonpath='{.spec.clusterIP}')
curl -X POST $BACKUP/backup/7            # device id 7 = p3 (idempotent: no-op commits nothing)
curl $BACKUP/backup/7/history            # commit list
curl $BACKUP/backup/7/config/latest      # file content at HEAD
```

### Restore a device (manual-approve gate)
```bash
RESTORE=http://$(kubectl -n restor8 get svc restor8-restore -o jsonpath='{.spec.clusterIP}')
curl $RESTORE/restore/7/diff/<sha>                      # review FIRST
curl -X POST "$RESTORE/restore/7/<sha>?approve=true"    # 403 without approve
curl -X POST "$RESTORE/restore/7/latest?approve=true"   # last good backup
```

Push rides a confirmed-commit window; failed validation auto-rolls-back
(`rollback_diff` in the response shows what was undone).

### Apply the topology plan (underlay)
```bash
TOPO=http://$(kubectl -n restor8 get svc restor8-topology -o jsonpath='{.spec.clusterIP}')
curl $TOPO/topology/reconcile     # plan ↔ inventory diff, "ready": true
curl -X POST $TOPO/topology/apply # interfaces + loopbacks + ASNs → all nodes
```

---

## 3. Change the topology plan (restor8 side)

File: `services/topology/app/topologies/mpls-core.yml`. Fields:

```yaml
nodes:
  - { name: p5, role: P, asn: 65005, loopback: 10.255.0.5, cleanup: [protocols bgp] }
links:
  - { a: p5, a_if: eth1, a_ip: 10.10.68.1/30, b: p2, b_if: eth6, b_ip: 10.10.68.2/30 }
```

Conventions (keep them — the scenario engine and diff views depend on them):

- `name` = the INVENTORY name (must match exactly; P-1 is uppercase, rest lowercase)
- `role` ∈ P / PE / RR / CE — drives mesh expectations and the UI
- loopback `10.255.0.x/32` (CEs `10.255.1.x`), unique ASN per node
- one /30 per link: `10.10.<4·i>.0/30`, side `a` gets `.1`, side `b` gets `.2`
- `a_if`/`b_if` MUST match hive's link order (see §5) — ethN = the Nth link
  of that node in `hive/04-routing/manifests/02-topology.yaml`
- `cleanup` = set-format `delete` lines applied before the rest (use for
  stale config; deleting an absent statement is a tolerated warning)

After editing:

```bash
git add -A && nix develop -c 'just deploy topology'   # plan is baked into the image
curl -X POST $TOPO/topology/apply
curl -X POST $BACKUP/backup/<id>                       # snapshot the new state
```

---

## 4. Add a cRPD node

**hive side** (fabric):

1. `04-routing/manifests/01-crpd-configs.yaml` — add `p5.conf` (copy a
   sibling; hostname + root-authentication hash + ssh/netconf only)
2. `04-routing/manifests/02-topology.yaml` — add the node under
   `definition.containerlab.topology.nodes` (`p5: { startup-config: /launchfiles/p5.conf }`)
   AND a `filesFromConfigMap` entry (`p5: [{ configMapName: crpd-configs, filePath: /launchfiles }]`)
3. `kubectl apply -f 01-crpd-configs.yaml -f 02-topology.yaml`
4. Wait for the launcher: `kubectl -n topology rollout status deploy/p5`

**restor8 side** (meaning):

5. Register in inventory:
   ```bash
   curl -X POST $GATE/api/devices -H 'content-type: application/json' \
     -d '{"name":"p5","mgmt_ip":"p5.topology.svc.cluster.local","port":830,
          "platform":"CRPD","auth_ref":"lab-auth-root","containerlab_node":"p5"}'
   ```
6. Add the node to `mpls-core.yml` (§3), deploy topology, `reconcile` →
   `ready: true`, `apply`, `backup`.

Verify: `curl -X POST $CONNECTOR/connect -d '{"host":"p5.topology...","auth_ref":"lab-auth-root"}'`
returns real facts.

---

## 5. Add a point-to-point link

**hive**: append to `02-topology.yaml` → `links:` —
`- endpoints: ["p5:eth1", "p2:eth6"]`. ethN = **next free index per node**
(the order defines the interface map — inserting mid-list renumbers
everything after it; only append). Apply, restart both launchers
(`kubectl -n topology rollout restart deploy/p5 deploy/p2`) so rpd picks
up the new interface.

**restor8**: append the link to `mpls-core.yml` (fresh /30 per §3),
deploy topology, `apply`, then prove it carries traffic:

```bash
curl -X POST $GATE/api/scenarios/bgp-fabric/run && curl $GATE/api/runs/<id>
# or check one device directly:
curl -X POST $CONNECTOR/snapshot -H 'content-type: application/json' \
  -d '{"host":"p5.topology.svc.cluster.local","auth_ref":"lab-auth-root",
       "rpc":"get-bgp-summary-information"}'   # peer-count / down-peer-count
```

---

## 6. Validate / health

```bash
just doctor       # k3s, registry, lab-auth secret, git index
just check        # ruff + mypy across everything
just test <svc>   # docker smoke per service
kubectl -n restor8 get pods      # 8 deployments, all 1/1 Running
curl $GATE/healthz
```

Live probes that touch real devices:

```bash
CONN=http://$(kubectl -n restor8 get svc restor8-connector -o jsonpath='{.spec.clusterIP}')
# facts (device alive + creds ok):
curl -X POST $CONN/connect -H 'content-type: application/json' \
  -d '{"host":"p2.topology.svc.cluster.local","auth_ref":"lab-auth-root"}'
# protocol state:
curl -X POST $CONN/snapshot -H 'content-type: application/json' \
  -d '{"host":"p2.topology.svc.cluster.local","auth_ref":"lab-auth-root",
       "rpc":"get-bgp-summary-information"}'   # peer-count=5, down-peer-count=0
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| NodePort/ClusterIP unreachable from host | k3s down | `sudo systemctl start k3s` |
| **No protocol forms adjacencies, eth1+ "not there"** | **MPLS kernel modules not loaded — the #1 silent killer after any REBOOT** | `sudo modprobe mpls_router mpls_iptunnel`, then `kubectl -n topology rollout restart deploy/p1 …deploy/ce2` (all ten); persist via `boot.kernelModules` in NixOS |
| ConnectAuthError on one node | credential drift (launcher restart resets to ConfigMap config) | check which `auth_ref` inventory points at; update the k8s Secret (`kubectl -n restor8 create secret generic lab-auth-root --from-literal=... --dry-run=client -o yaml | kubectl apply -f -`) + redeploy connector; for durable fix put the hash in hive's `01-crpd-configs.yaml` |
| Whole node "stock" (no lo0/AS/interfaces) | launcher restarted → ConfigMap startup-config | idempotent: `curl -X POST $TOPO/topology/apply`, then backup |
| `just build` ships old code | flakes read the git INDEX | `git add -A` before build/deploy — always |
| ImagePullBackOff | image never pushed (bare `kubectl apply`), or registry down | `just deploy <svc>` (chains push); `curl -s localhost:5000/v2/` |
| Push fails "syntax error", bad_element = NEXT line | brace-less `}}}}` in text payloads, or `delete` lines in text format | payloads are set-format in the app — if writing new renderers, keep them set-format and space your braces |
| Push reports UnlockError but config landed | cRPD closes the candidate DB after confirming commit | handled (best-effort unlock) — if you see it again, check the ERROR events for the REAL first failure |
| Scenario "0 established" but routes exist | cRPD terse summary has no per-peer state; counting parsed whitespace | handled (header `peer-count`/`down-peer-count`) — don't regress to string-counting `<peer-state>` |
| jsnapy errors `Read-only file system: /app/.jsnapy` | jsnapy writes `~/.jsnapy` | `HOME=/tmp` is set in the scenario deployment — keep it |
| scenario `statement not found` load failure | deleting absent config (fresh node) | handled (`ignore_warning`) — keep it on loads |
| Restore 403 | working as designed | review the diff endpoint, then `?approve=true` |
| Restore "Method Not Found"-ish upstream errors | an upstream service wasn't redeployed after its API changed | redeploy the stale service (`just deploy backup/restore/...`) |
| Port 1808x already in use | stale port-forward | `pgrep -af kubectl.*port-forward` + kill |
| Ingress 404 right after apply | Traefik hasn't reconciled | wait a few seconds |
| Frontend shows shell, no data | browsing an origin without the API behind it | use the frontend pod IP / ClusterIP / restor8.home (nginx proxies /api) — never a bare static serve |
| Pod IP stopped working | pod was redeployed (ephemeral by design) | re-fetch the IP, or use the ClusterIP |
| PVC data "lost" after pod delete | it isn't — SQLite/git live on PVCs | nothing to do; verify with a backup history call |

**Where the bodies are buried** (event streams + run details):
`just logs connector` (device events as JSON), `just logs scenario`
(phase records), `kubectl -n restor8 logs deploy/restor8-<svc> --previous`
(last crash), `GET $GATE/api/runs/<id>` (full phase/node/jsnapy detail).

---

## 8. Everyday command card

```bash
# from repo root (python services):
just deploy <svc>          # build → push → rollout (connector|inventory|backup|restore|topology|scenario|gateway)
just run <svc> · just logs <svc> · just test <svc>
just check · just doctor · just shell

# frontend (own flake):
cd frontend && just deploy · just relock (after npm dep changes)

# hive fabric:
cd ~/github/ngeran/hive/04-routing && kubectl apply -f manifests/01-crpd-configs.yaml -f manifests/02-topology.yaml
```
