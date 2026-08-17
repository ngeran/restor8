# restor8

Home-lab network automation for Juniper gear (cRPD, vJunos-router, MX, ACX)
on the single-node k3s cluster, alongside Containerlab topologies.

Four jobs: **configure** (templated protocol pushes — BGP, ISIS, OSPF,
LDP-TE, MPLS, L3VPN, TWAMP Light), **backup** (running config → a local Git
repo, config-as-history), **restore** (diff a historical config against
running, push it back behind a validation gate), **test** (named scenarios
pushed, awaited for convergence, and validated with JSNAPy).

Architecture, phase order, and locked decisions (shared lab credential,
manual-approve/auto-rollback restores, Junos-only, root flake + uv
workspace): see **TODO.md** — it is the build ledger.

```
├── flake.nix / justfile / uv.lock   # one root flake → per-service images
├── libs/restor8_core/               # shared uv package: PyEZ wrapper, events, models
├── services/<name>/                 # app code + pyproject (virtual uv member)
├── manifests/<name>/                # per-service k8s (ns: restor8, port 8080)
└── frontend/                        # React + Tailwind v4 (Phase 7)
```

**connector** is the only service that opens NETCONF sessions; everything
else (backup, restore, scenario) reaches devices through its REST API and
consumes its progress events (`resolving → connecting → … → commit-confirmed`)
via the gateway's WebSocket fan-out.

## Run / deploy

```bash
just run                 # local dev: uvicorn with --reload (default svc: connector)
just deploy              # build → push → k3s (ns restor8);  just deploy <svc>
just check               # ruff (strict) + mypy (advisory)
just doctor              # k3s + registry + lab-auth secret + git-index pre-flight
```

**Full deploy runbook with explanations: [DEPLOY.md](DEPLOY.md)** — namespace,
credential Secret, deploy, in-cluster wire test, event stream, troubleshooting.

**Operating manual: [RUNBOOK.md](RUNBOOK.md)** — using the app, changing the
topology plan, adding cRPDs and point-to-point links (hive + restor8 sides),
validation, and the troubleshooting table.

Deps live in `pyproject.toml` + one `uv.lock` for the whole workspace —
edit, `uv lock`, `just build`. No requirements.txt, ever.

Lab credential (one-time, in-cluster):

```bash
kubectl -n restor8 create secret generic lab-auth \
  --from-literal=LAB_USER=<user> --from-literal=LAB_PASSWORD=<pass>
```

**Wire test (Phase 0 checkpoint):** `just run`, then against a real node —

```bash
curl -s localhost:8080/connect -H 'content-type: application/json' \
  -d '{"host": "<node-mgmt-ip>", "user": "<lab-user>", "auth": "<lab-pass>"}'
```

Expect real facts (`hostname`, `model`, `version`) and the JSON event
sequence (`resolving … connected … closed`) in the service logs.
