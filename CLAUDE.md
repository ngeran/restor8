# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

restor8 — home-lab Juniper automation (configure / backup / restore / protocol-test) for cRPD, vJunos-router, MX, ACX on the local k3s cluster. Microservices share one library; `TODO.md` is the authoritative phase ledger with locked decisions — **do not start a phase until the previous checkpoint passed against a real device**, and don't re-ask decided questions (auth, restore gating, vendor scope, repo layout are all recorded there).

## Layout (one root flake + uv workspace)

```
├── flake.nix               # ONE flake: serviceApps registry → per-service images/devshells
├── pyproject.toml          # virtual uv workspace root (members: libs/*, services/*)
├── uv.lock                 # single lock for the whole workspace
├── justfile                # every recipe takes a service arg (default: connector)
├── libs/restor8_core/      # real package (hatchling) — the ONLY place junos-pyEZ lives
├── services/<name>/        # virtual uv members: pyproject + app/ (code only, no flake)
└── manifests/<name>/       # per-service Deployment+Service, ns restor8, port 8080
```

Add a service: `services/<name>/pyproject.toml` (virtual member, `restor8-core = { workspace = true }`) → add to `serviceApps` in flake.nix → `manifests/<name>/` → `uv lock` → `git add -A` → `just deploy <name>`.

## Architecture rules that matter

- **connector is the only service that touches devices.** backup/restore/scenario call its REST API — they never import PyEZ (enforced structurally: only `restor8-core` declares `junos-pyEZ`, and only connector depends on the parts that use it).
- **Everything is a progress-event stream.** `JunosConnection` (libs/restor8_core/src/restor8_core/junos.py) emits `DeviceEvent`s at every stage (`resolving → connecting → authenticating → connected → locking → loading-config → diff-ready → committing → commit-confirmed → … → closed`, plus `error`). New device operations must emit events — live feedback is the core UX requirement, not plumbing.
- **Config pushes are always `lock → load → diff → commit confirmed`** (default 2-minute window). The only permanent commits are `confirm_commit()` (success-gated) and `rollback()` (restores known-good). Never add a bare `commit()`.
- **PyEZ exceptions get mapped** via `map_pyez_error` (libs/restor8_core/src/restor8_core/models.py) to typed errors with the Junos message verbatim — never swallow or summarize them away.
- Every RPC call in junos.py carries an inline `# equivalent to: show …` comment naming the CLI command. Keep that convention.

## Commands

```bash
just run [svc]        # local dev, same entrypoint as the image (uvicorn main:app, --reload)
just build [svc]      # nix build .#svc --out-link result-svc (default svc: connector)
just push [svc]       # skopeo → localhost:5000/restor8-svc:latest (plain HTTP, no docker)
just deploy [svc]     # ns + manifests/svc/ → rollout restart + status (fails loudly)
just logs [svc] · just forward [svc]   # tail / port-forward 8080
just check            # ruff (strict) + mypy (advisory) over libs/ + services/
just test [svc]       # docker smoke: load image, run, curl /healthz, expect 200
just doctor           # k3s + registry + lab-auth secret + git-index pre-flight
```

No test suite yet — `just check` and `just test` are the verification commands; phase checkpoints against real devices are the real gates.

## Dependencies & pipeline gotchas

- `pyproject.toml` + `uv.lock` are the single source for dev shells AND images (uv2nix). Change deps → `uv lock` (in devShell or `nix run nixpkgs#uv -- lock`) → `just build`. Never `pip install`, never requirements.txt.
- **`git add -A` before `just build`/`just deploy`** — nix evaluates the git *index*, not the worktree; unstaged edits to `app/`, `flake.nix`, or new services are invisible to the build.
- `direnv allow` after editing `flake.nix` or `.envrc`.
- Image name/tag stay in lockstep: `flake.nix` (`localhost:5000/restor8-<svc>:latest`) ↔ `manifests/<svc>/*.yaml` ↔ justfile. Fixed `:latest` + `imagePullPolicy: Always` is intentional.
- Images run non-root UID 1000 (baked `/etc/passwd` appuser), read-only rootfs, dropped caps, seccomp=RuntimeDefault — keep new manifests in that style. Avoid port 5000 (it's the registry).
- Shared lab credential = k8s Secret `restor8/lab-auth` → env `LAB_USER`/`LAB_PASSWORD` (connector falls back to it when a request omits creds). Never bake credentials into the repo.
- **In-cluster device addressing:** a pod reaching a containerlab node uses the k3s node IP (`10.0.0.29`, NodePort-style ports like `31001`), never `localhost` — `localhost` inside a pod is the pod itself. `kubectl -n restor8 port-forward svc/restor8-<svc> 18080:8080` + `just logs <svc>` is the test loop from the cluster.
