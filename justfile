# =========================================================================
# restor8 — per-service build → push → deploy on the single root flake.
# =========================================================================
# Every recipe takes the service name (default: connector):
#     just deploy            # == just deploy connector
#     just deploy backup     # (once Phase 2 adds it to flake serviceApps)
# Requires omni-nix's k3s cluster + local registry (localhost:5000).
# GOLDEN RULE: flakes read the git INDEX — `git add -A` before build/deploy.
# =========================================================================
set shell := ["bash", "-c"]

ns := "restor8"

# Build the service's Nix image (no Dockerfile, no docker).
build svc="connector":
    nix build .#{{svc}} --out-link result-{{svc}}

# Push to the local registry over HTTP (no docker). `result-<svc>` resolves
# to the image tarball dockerTools.buildImage produces.
push svc="connector": (build svc)
    #!/usr/bin/env bash
    set -euo pipefail
    skopeo copy --insecure-policy --dest-tls-verify=false \
      docker-archive:"$(readlink -f result-{{svc}})" \
      docker://localhost:5000/restor8-{{svc}}:latest

# Apply namespace + the service's manifests, restart the rollout, and fail
# loudly (pod status + last crash log) if it doesn't come up.
deploy svc="connector": (push svc)
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl apply -f manifests/namespace.yaml
    kubectl apply -f manifests/ingress.yaml
    kubectl apply -f manifests/{{svc}}/
    kubectl -n {{ns}} rollout restart deployment/restor8-{{svc}}
    kubectl -n {{ns}} rollout status deployment/restor8-{{svc}} --timeout=120s || {
      echo "FAIL rollout - pod status + last crash log:"
      kubectl -n {{ns}} get pods
      kubectl -n {{ns}} logs deployment/restor8-{{svc}} --previous --tail=40
      exit 1
    }

# Run locally — the SAME entrypoint the image runs (uvicorn main:app from
# services/<svc>/app) with --reload. Enters the service's OWN devshell
# first so each service runs against its own venv, no matter which
# devshell your terminal happens to be in.
run svc="connector":
    nix develop .#{{svc}} -c bash -c 'cd services/{{svc}}/app && uvicorn main:app --reload --port 8080'

# Tail the service's deployment logs (Job pods: kubectl logs job/<name>).
logs svc="connector":
    kubectl -n {{ns}} logs deploy/restor8-{{svc}} -f

# Port-forward the service (localhost:8080 → pod).
forward svc="connector":
    kubectl -n {{ns}} port-forward svc/restor8-{{svc}} 8080:8080

# Pre-flight: k3s up, local registry reachable, lab-auth secret present,
# git index clean (nix evaluates the git INDEX — stage or build is blind).
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    ok=1
    systemctl is-active --quiet k3s && echo "  k3s        up" || { echo "  k3s        DOWN -> sudo systemctl start k3s"; ok=0; }
    curl -sf --max-time 3 http://localhost:5000/v2/ >/dev/null && echo "  registry   localhost:5000 reachable" || { echo "  registry   UNREACHABLE -> start k3s / the registry"; ok=0; }
    kubectl -n {{ns}} get secret lab-auth >/dev/null 2>&1 && echo "  lab-auth   secret present" || echo "  lab-auth   WARN - missing (POST /connect needs creds until created): kubectl -n {{ns}} create secret generic lab-auth --from-literal=LAB_USER=... --from-literal=LAB_PASSWORD=..."
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      # porcelain XY: X=index, Y=worktree. Only Y-side (or untracked ??)
      # entries are invisible to nix — staged-not-yet-committed is fine.
      n=$(git status --porcelain . 2>/dev/null | grep -cE '^(.[MDAU?]|\?\?)')
      [ "$n" = 0 ] && echo "  git        clean" || echo "  git        WARN - $n unstaged/untracked here (nix uses the git INDEX: stage with git add or nix build ignores them)"
    else
      echo "  git        (not a worktree - skip)"
    fi
    [ "$ok" = 1 ] && echo "doctor: ready" || { echo "doctor: NOT ready"; exit 1; }

# Local smoke: load the built image into docker, run it, curl /healthz.
test svc="connector": (build svc)
    #!/usr/bin/env bash
    set -euo pipefail
    command -v docker >/dev/null || { echo "test needs docker (virtualization.nix)"; exit 1; }
    img=$(docker load < "$(readlink -f result-{{svc}})" | sed -n 's/Loaded image: \(.*\)/\1/p')
    echo "loaded $img"
    docker rm -f restor8-{{svc}}-test >/dev/null 2>&1 || true
    docker run -d --name restor8-{{svc}}-test --tmpfs /tmp:mode=1777,uid=1000,gid=1000 -p 18080:8080 "$img" >/dev/null
    sleep 2
    if curl -sf --max-time 5 http://localhost:18080/healthz >/dev/null; then
      echo "  HTTP 200 from /healthz  OK"
    else
      echo "  FAIL no 200 - container logs:"; docker logs restor8-{{svc}}-test 2>&1 | tail -25; docker rm -f restor8-{{svc}}-test >/dev/null; exit 1
    fi
    docker rm -f restor8-{{svc}}-test >/dev/null
    echo "test: ok"

# Lint + type-check everything (the image build doesn't lint). ruff is
# strict. mypy runs per service against that service's own venv so each
# one sees exactly its dependencies (libs are checked with every service).
check:
    #!/usr/bin/env bash
    set -euo pipefail
    ruff check libs services
    for d in services/*/app; do
      svc="$(basename "$(dirname "$d")")"
      echo "── mypy [$svc]"
      nix develop .#"$svc" -c bash -c \
        "mypy --python-executable \"\$(command -v python)\" libs/restor8_core/src $d"
    done

# Drop into the default devShell manually (direnv does this on cd).
shell:
    nix develop
