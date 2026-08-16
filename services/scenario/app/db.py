"""SQLite store for scenario run history.

Why a DB for runs (spec §2): the UI's timeline needs "last run per
scenario, pass/fail, when, with what per-node detail" — that's a query,
not a log grep. Definitions stay YAML-in-repo (they're code); only
OUTCOMES live here.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario    TEXT NOT NULL,
    status      TEXT NOT NULL,   -- running | passed | failed
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    detail      TEXT NOT NULL DEFAULT '{}'   -- JSON: nodes, phases, jsnapy
)
"""


class RunDB:
    """Thread-safe SQLite wrapper (same pattern as inventory)."""

    def __init__(self, path: str | None = None) -> None:
        """Open/create at ``path`` (env SCENARIO_DB; /tmp default keeps
        read-only-rootfs containers and local runs working; the k8s
        manifest points it at a PVC)."""
        self._path = path or os.environ.get("SCENARIO_DB", "/tmp/scenario.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def start(self, scenario: str) -> int:
        """Insert a run row in 'running' state; returns its ID."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (scenario, status) VALUES (?, 'running')",
                (scenario,),
            )
            self._conn.commit()
            rowid = cur.lastrowid
        assert rowid is not None  # noqa: S101 — INSERT always yields one
        return rowid

    def finish(self, run_id: int, status: str, detail: str) -> None:
        """Stamp the run's outcome (passed/failed) + detail JSON."""
        with self._lock:
            self._conn.execute(
                """UPDATE runs SET status = ?, finished_at =
                   strftime('%Y-%m-%dT%H:%M:%fZ','now'), detail = ?
                   WHERE id = ?""",
                (status, detail, run_id),
            )
            self._conn.commit()

    def get(self, run_id: int) -> dict[str, Any] | None:
        """One run row as a dict (detail kept as JSON string)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, scenario: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Recent runs, newest first (optionally one scenario)."""
        q = "SELECT * FROM runs"
        args: tuple[Any, ...] = ()
        if scenario:
            q += " WHERE scenario = ?"
            args = (scenario,)
        q += " ORDER BY id DESC LIMIT ?"
        args += (limit,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]
