"""backup — pull running configs into a Git repo, config-as-history (Phase 2).

Why this service exists: a backup blob answers "what was it?", but the
mockup's commit/diff/revert UI needs *history* — so every backup is a
commit in a plain Git repo, one file per device
(``devices/<name>/running.cfg``), giving diff and revert almost for free
in Phase 3/7.

Architecture (spec §2): backup NEVER imports PyEZ. It chains two HTTP
calls — inventory (who is this device?) → connector (pull its config) —
then commits locally. Credentials never appear here: connector resolves
them from the device's ``auth_ref``.
"""

from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from git import Actor, Repo
from pydantic import BaseModel

INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://restor8-inventory:8080")
CONNECTOR_URL = os.environ.get("CONNECTOR_URL", "http://restor8-connector:8080")
REPO_PATH = os.environ.get("BACKUP_REPO", "/tmp/backup-repo")
"""PVC mount in-cluster (/data/repo); /tmp default keeps local `just run`
and the docker smoke test working on read-only root filesystems."""

_ACTOR = Actor("restor8", "restor8@lab")
_git_lock = threading.Lock()
"""Serialises write+add+commit across the threadpool — the repo index is
shared state; two concurrent backups would race it. A one-user lab never
notices the lock, but correctness is cheap here."""

app = FastAPI(
    title="restor8 backup",
    description="Config-as-history: pull device configs into a Git repo.",
    version="0.1.0",
)


class BackupResult(BaseModel):
    """Outcome of one backup run."""

    device: str
    commit: str | None
    """Short SHA of the new commit, or null when nothing changed."""

    changed: bool
    path: str


class HistoryEntry(BaseModel):
    """One commit touching a device's config file."""

    sha: str
    date: str
    message: str


def _repo() -> Repo:
    """Open the backup repo, initialising it on first use.

    Returns:
        A GitPython Repo rooted at ``REPO_PATH`` with restor8's identity.

    Raises:
        OSError: the repo path can't be created (missing mount etc.).
    """
    Path(REPO_PATH).mkdir(parents=True, exist_ok=True)
    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        return Repo(REPO_PATH)
    repo = Repo.init(REPO_PATH)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "restor8")
        cw.set_value("user", "email", "restor8@lab")
    # An empty root commit gives HEAD something to diff against — without
    # it, the first backup's "did anything change?" check has no baseline.
    repo.index.commit("backup repo initialised", author=_ACTOR, committer=_ACTOR)
    return repo


def _device(device_id: int) -> dict[str, object]:
    """Look up a device in inventory.

    Args:
        device_id: inventory primary key.

    Returns:
        The device row (name, mgmt_ip, port, auth_ref, …).

    Raises:
        HTTPException 404: no such device.
        HTTPException 502: inventory unreachable/errored.
    """
    try:
        r = httpx.get(f"{INVENTORY_URL}/devices/{device_id}", timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"inventory unreachable at {INVENTORY_URL}: {exc}"
        ) from exc
    if r.status_code == 404:
        raise HTTPException(404, f"device {device_id} not found in inventory")
    if r.status_code != 200:
        raise HTTPException(502, f"inventory error {r.status_code}: {r.text[:200]}")
    return r.json()


def _pull_config(device: dict[str, object], fmt: str) -> str:
    """Pull a device's running config through connector.

    Args:
        device: inventory row (mgmt_ip, port, auth_ref).
        fmt: config format — kept stable per device so Git diffs stay
            meaningful (spec §4).

    Returns:
        The configuration text.

    Raises:
        HTTPException 502: connector unreachable or the device errored —
            connector's typed error (device/stage/message) is passed
            through untouched.
    """
    try:
        r = httpx.post(
            f"{CONNECTOR_URL}/config",
            json={
                "host": device["mgmt_ip"],
                "port": device["port"],
                "auth_ref": device["auth_ref"],
                "fmt": fmt,
            },
            timeout=180,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"connector unreachable at {CONNECTOR_URL}: {exc}"
        ) from exc
    if r.status_code != 200:
        # Pass connector's structured error through verbatim.
        raise HTTPException(502, r.json().get("detail"))
    return r.json()["config"]


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — smoke-test 200 target."""
    return {"service": "restor8-backup", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target."""
    return {"status": "ok"}


@app.post("/backup/{device_id}", response_model=BackupResult)
def backup_device(device_id: int, fmt: str = "text") -> BackupResult:
    """Back up one device: pull config → commit (idempotent).

    Args:
        device_id: inventory ID.
        fmt: ``text`` (default) or ``set`` — must stay stable per device.

    Returns:
        The commit SHA and changed-flag. No commit is made when the
        config is identical to the last backup.
    """
    device = _device(device_id)
    config = _pull_config(device, fmt)

    rel = f"devices/{device['name']}/running.cfg"
    absolute = Path(REPO_PATH) / rel
    with _git_lock:
        absolute.parent.mkdir(parents=True, exist_ok=True)
        absolute.write_text(config)
        repo = _repo()
        repo.index.add([rel])
        if not repo.index.diff("HEAD"):
            # Identical content — a commit would pollute history with
            # no-op entries; the UI's timeline stays meaningful.
            return BackupResult(
                device=str(device["name"]), commit=None, changed=False, path=rel
            )
        ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        commit = repo.index.commit(
            f"backup: {device['name']} @ {ts}", author=_ACTOR, committer=_ACTOR
        )
    return BackupResult(
        device=str(device["name"]), commit=commit.hexsha[:12], changed=True, path=rel
    )


class BackupContent(BaseModel):
    """A device's config file content at one commit (restore's source)."""

    device: str
    sha: str
    date: str
    config: str


@app.get("/backup/{device_id}/config/{sha}", response_model=BackupContent)
def config_at(device_id: int, sha: str) -> BackupContent:
    """The device's backed-up config at a commit — restore's data source.

    Args:
        device_id: inventory ID.
        sha: commit SHA (short or full) or the literal ``latest``.

    Returns:
        The config text as stored at that commit.

    Raises:
        HTTPException 404: unknown device, unknown commit, or the device
            had no backup at that commit.
    """
    device = _device(device_id)
    rel = f"devices/{device['name']}/running.cfg"
    with _git_lock:
        repo = _repo()
        commit = repo.head.commit if sha == "latest" else repo.commit(sha)
        try:
            blob = commit.tree / rel
            content = blob.data_stream.read().decode()
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                404, f"no backup for {device['name']} at {commit.hexsha[:12]}"
            ) from exc
        date = commit.committed_datetime.isoformat()
        hexsha = commit.hexsha[:12]
    return BackupContent(device=str(device["name"]), sha=hexsha, date=date, config=content)


@app.get("/backup/{device_id}/history", response_model=list[HistoryEntry])
def history(device_id: int) -> list[HistoryEntry]:
    """Commit history for one device's config file.

    Args:
        device_id: inventory ID.

    Returns:
        Commits newest-first (sha, date, message).

    Raises:
        HTTPException 404: no such device (and no history yet for it).
    """
    device = _device(device_id)
    rel = f"devices/{device['name']}/running.cfg"
    with _git_lock:
        repo = _repo()
        if not (Path(REPO_PATH) / rel).exists():
            return []
        commits = list(repo.iter_commits(paths=rel, max_count=100))
    entries: list[HistoryEntry] = []
    for c in commits:
        # GitPython yields commit messages as str or bytes — normalise.
        m = c.message
        entries.append(
            HistoryEntry(
                sha=c.hexsha[:12],
                date=c.committed_datetime.isoformat(),
                message=(m.decode() if isinstance(m, bytes) else m).strip(),
            )
        )
    return entries
