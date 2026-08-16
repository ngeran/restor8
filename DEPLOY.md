# Deploying restor8 to the k3s cluster — step by step

The complete runbook for taking a service from source to running pod, then
proving it works from inside the cluster. Written for connector (Phase 0)
but the steps are identical for every service — swap the name.

> **Read this first — the three ideas the whole flow hangs on:**
> 1. **Nix builds from the git index.** Unstaged/untracked files are
>    invisible to `just build`/`just deploy`. `git add -A` first, always.
> 2. **`localhost:5000` is the registry from containerd's point of view.**
>    `skopeo` pushes to it on the host; k3s's containerd has it configured
>    as an insecure mirror, so image names like
>    `localhost:5000/restor8-connector:latest` resolve inside the cluster.
> 3. **Addresses are relative to where you run them.** Your laptop reaches
>    the cRPD at `localhost:31001`. A *pod* must use the node IP
>    (`10.0.0.29:31001`) — `localhost` inside a pod is the pod itself.

---

## Step 0 — Pre-flight: `just doctor`

```bash
just doctor
```

Checks the four silent-failure sources before you waste a build:

| Check | Meaning | If it fails |
|---|---|---|
| `k3s up` | the cluster daemon is running | `sudo systemctl start k3s` |
| `registry reachable` | `localhost:5000` answers `/v2/` | start k3s (the registry rides on it) |
| `lab-auth present` | the credential Secret exists (ns `restor8`) | Step 2 below |
| `git clean` | nothing unstaged that Nix would miss | `git add -A` |

All four green → `doctor: ready`. **Never deploy with a WARN on k3s or the
registry** — you'll get ImagePullBackOff or a push failure.

## Step 1 — Create the namespace

```bash
kubectl apply -f manifests/namespace.yaml
# namespace/restor8 created   (or "unchanged" — apply is idempotent)
```

The namespace must exist **before** the Secret and the service manifests,
because both live inside it. `just deploy` re-applies this file every time,
so you only ever run this by hand for the first deploy or after a teardown.

## Step 2 — Create the lab credential Secret (one-time)

```bash
kubectl -n restor8 create secret generic lab-auth \
  --from-literal=LAB_USER=admin \
  --from-literal=LAB_PASSWORD=manolis1
```

What happens: the Deployment's `env:` block has a `secretKeyRef` for each
key; at pod start Kubernetes injects them as `LAB_USER`/`LAB_PASSWORD`.
The connector uses them when a `/connect` request omits credentials —
which is how services will talk to devices without ever seeing a password.

To **rotate or change** the credential later (plain `create` errors with
"already exists" — that's the idempotent update idiom):

```bash
kubectl -n restor8 create secret generic lab-auth \
  --from-literal=LAB_USER=admin \
  --from-literal=LAB_PASSWORD=<new> \
  --dry-run=client -o yaml | kubectl apply -f -
# then restart so running pods pick up the new value:
kubectl -n restor8 rollout restart deployment/restor8-connector
```

> Secrets are **base64, not encrypted** (`kubectl get secret lab-auth -o
> jsonpath='{.data.LAB_USER}' | base64 -d` prints the value). Right for a
> home lab; if this ever leaves the lab, move to SOPS/sealed-secrets first.

## Step 3 — Deploy: `just deploy connector`

```bash
just deploy connector
```

One recipe, four stages — know what each is doing:

1. **`just build`** → `nix build .#connector`: uv2nix builds the venv from
   `uv.lock` and `dockerTools.buildImage` assembles the OCI image (no
   Dockerfile, no docker daemon). Stale if you forgot `git add -A`!
2. **`just push`** → `skopeo copy` ships the image tarball into
   `localhost:5000` over plain HTTP (`--dest-tls-verify=false`).
3. **`kubectl apply`** of `manifests/namespace.yaml` + `manifests/connector/`.
4. **`rollout restart` + `rollout status`** — the gate. The restart forces
   a new ReplicaSet even though the tag is fixed (`:latest`), because the
   manifest sets `imagePullPolicy: Always` → containerd re-fetches the
   digest you just pushed. If the pod doesn't report ready within 120s,
   the recipe dumps `get pods` + the last crash log and exits non-zero.

Success looks like:

```
deployment.apps/restor8-connector created
service/restor8-connector created
deployment.apps/restor8-connector restarted
Waiting for deployment "restor8-connector" rollout to finish...
deployment "restor8-connector" successfully rolled out
```

## Step 4 — Verify the pod

```bash
kubectl -n restor8 get pods -o wide
# NAME                               READY  STATUS   RESTARTS  AGE  IP           NODE
# restor8-connector-5c7bc55f7-rvkqs  1/1    Running  0         40s  10.42.0.186  nixos-btw
```

`1/1 Running` means both containers-started AND readiness-probe-passing
(`/healthz`). If it's stuck `0/1`, it's crash-looping or failing probes →
`kubectl -n restor8 logs deploy/restor8-connector --previous`.

## Step 5 — Test from inside the cluster

**5a. Open the tunnel (own terminal, leave it running).**
`port-forward` pipes a local port through the API server to the Service:

```bash
kubectl -n restor8 port-forward svc/restor8-connector 18080:8080
# Forwarding from 127.0.0.1:18080 -> 8080
```

It *blocks* — that's normal. A curl that returns nothing almost always
means this isn't running (or died when you Ctrl-C'd it). `just forward`
does the same on 8080.

**5b. Find the device's cluster-side address.** The cRPD's SSH port is
published on the *host*, so pods reach it via the node IP:

```bash
kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}'
# 10.0.0.29
```

**5c. Fire the same wire test — with NO credentials in the request** (this
is what proves the Secret → env → connector path):

```bash
curl -s localhost:18080/connect -H 'content-type: application/json' \
  -d '{"host":"10.0.0.29","port":31001}'
```

Expected: real facts —

```json
{"session_id":"eb3c83dd47ad","facts":{"hostname":"P-1","model":"CRPD","version":"25.4R1-S2.3",...}}
```

Passing creds explicitly (`"user":"admin","auth":"..."`) also works and
overrides the Secret — useful for per-device exceptions later.

## Step 6 — Watch the event stream

```bash
just logs connector        # or: kubectl -n restor8 logs deploy/restor8-connector -f
```

You should see one JSON line per pipeline stage:

```
{"stage":"resolving","message":"probing 10.0.0.29:31001",...}
{"stage":"resolving","message":"device reachable","detail":{"latency_ms":1.1},...}
{"stage":"connecting","message":"opening SSH/NETCONF session",...}
{"stage":"authenticating","message":"authenticating as admin",...}
{"stage":"connected","message":"connected to P-1 (CRPD 25.4R1-S2.3)",...}
{"stage":"closed","message":"session closed",...}
```

This stream is the product: the same objects the gateway will fan out over
WebSocket in Phase 6.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ImagePullBackOff` | registry down, or you deployed before pushing (`just deploy` chains both — a bare `kubectl apply` doesn't) | `just doctor`; then `just deploy connector` |
| Pod built old code | you edited files but didn't `git add -A` — Nix saw the stale index | `git add -A && just deploy connector` |
| `CrashLoopBackOff` | app error at startup | `kubectl -n restor8 logs deploy/restor8-connector --previous` |
| curl to :18080 returns nothing | no port-forward running | Step 5a — separate terminal, leave open |
| `422 no credentials` | Secret missing AND request had no creds | Step 2 |
| `502 DeviceUnreachableError: TCP probe ... failed` | wrong address from the pod (`localhost:31001`) | use the node IP: `10.0.0.29:31001` |
| `502 AuthenticationFailedError` | credential mismatch | rotate Secret (Step 2) or pass `user`/`auth` explicitly |
| `rollout status` timeout in `just deploy` | readiness probe failing | recipe prints pods + crash log; check `/healthz` responds locally via port-forward |
| `secret "lab-auth" already exists` | re-running `create` | use the `--dry-run=client -o yaml | kubectl apply -f -` update idiom (Step 2) |

## Day-2 operations

```bash
# change code → redeploy (the whole loop)
git add -A && just deploy connector

# logs / shell / teardown
just logs connector
kubectl -n restor8 exec -it deploy/restor8-connector -- sh   # image has no shell by design — expect this to fail
kubectl -n restor8 delete deployment,svc --all               # teardown services (keeps ns + secret)
```

## Exposing via the host browser (Ingress)

`manifests/ingress.yaml` routes hostnames through k3s's Traefik — applied
by every `just deploy`. Map the names once (sudo, one-time):

```bash
sudo sh -c 'echo "10.0.0.29 connector.restor8.home inventory.restor8.home restor8.home" >> /etc/hosts'
```

Then browse: `http://connector.restor8.home/docs` (Swagger UI),
`http://inventory.restor8.home/devices` (live registry JSON). The Phase 7
frontend will live at `http://restor8.home`.

## Deploying the *next* service (from Phase 1 on)

The steps are already generalized — nothing above is connector-specific
except the name:

```bash
# one-time per new service:
#   1. services/<name>/pyproject.toml + app/
#   2. flake.nix → serviceApps += <name>
#   3. manifests/<name>/{deployment,service}.yaml (copy connector's; same
#      securityContext, probes, resources — change name/image/labels only)
#   4. uv lock
just deploy <name>
kubectl -n restor8 get pods
```
