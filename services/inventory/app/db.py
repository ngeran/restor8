"""SQLite store for the device inventory.

Why SQLite (spec §2: "keep it boring — this is a home lab"): one file on a
small PVC, no operator, no secrets engine, and single-writer semantics that
a one-person lab never outgrows. The registry holds *what exists and how to
address it* (mgmt_ip, port, auth_ref); it deliberately knows nothing about
live sessions — that's connector's half of the split.

Address semantics worth stating once: ``mgmt_ip`` is the address of the
device **as reachable from inside the cluster**. For containerlab nodes
published on the host that is the k3s node IP + published port (e.g.
``10.0.0.29:31001``), NOT ``localhost:31001`` — a pod's localhost is the
pod itself (see DEPLOY.md).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    mgmt_ip            TEXT    NOT NULL,
    port               INTEGER NOT NULL DEFAULT 830,
    platform           TEXT    NOT NULL DEFAULT '',
    auth_ref           TEXT    NOT NULL DEFAULT 'lab-auth',
    containerlab_node  TEXT,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


class InventoryDB:
    """Thread-safe wrapper over one SQLite connection.

    FastAPI runs sync endpoints in a threadpool, so every call funnels
    through a lock — SQLite is happiest single-writer, and inventory is
    far too small for that to ever matter.
    """

    def __init__(self, path: str | None = None) -> None:
        """Open (or create) the database at ``path``.

        Args:
            path: filesystem path; defaults to ``$INVENTORY_DB`` or
                ``/tmp/inventory.db``. The tmp default is deliberate: the
                container's root fs is read-only, so a relative default
                would crash on startup — /tmp is writable in both the
                image (tmpfs) and any dev environment. Persistent state
                comes from the k8s manifest setting INVENTORY_DB to the
                PVC path (/data/inventory.db).
        """
        self._path = path or os.environ.get("INVENTORY_DB", "/tmp/inventory.db")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL: concurrent readers don't block behind the writer.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── reads ────────────────────────────────────────────────────────

    def list_devices(self) -> list[dict[str, Any]]:
        """Return all devices, registration order.

        Returns:
            List of device rows as plain dicts (JSON-ready).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        """Fetch one device by ID.

        Args:
            device_id: primary key.

        Returns:
            The row as a dict, or ``None`` when absent.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        """Fetch one device by (case-insensitive) name.

        Args:
            name: device name, e.g. ``P-1``.

        Returns:
            The row as a dict, or ``None`` when absent.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
        return dict(row) if row else None

    # ── writes ───────────────────────────────────────────────────────

    def create_device(self, device: dict[str, Any]) -> dict[str, Any]:
        """Insert a device.

        Args:
            device: column values (name, mgmt_ip, port, platform,
                auth_ref, containerlab_node).

        Returns:
            The inserted row including id/created_at.

        Raises:
            sqlite3.IntegrityError: duplicate name (mapped to 409 by the
                API layer).
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO devices (name, mgmt_ip, port, platform, auth_ref, containerlab_node)
                   VALUES (:name, :mgmt_ip, :port, :platform, :auth_ref, :containerlab_node)""",
                device,
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def update_device(
        self, device_id: int, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Patch selected columns of one device.

        Args:
            device_id: primary key.
            fields: only the columns to change (non-empty, validated by
                the API layer).

        Returns:
            The updated row, or ``None`` when the ID doesn't exist.
        """
        sets = ", ".join(f"{col} = :{col}" for col in fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE devices SET {sets} WHERE id = :id",  # noqa: S608 — cols whitelisted by API layer
                {**fields, "id": device_id},
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return dict(row)

    def delete_device(self, device_id: int) -> bool:
        """Delete one device.

        Args:
            device_id: primary key.

        Returns:
            True when a row was removed, False when the ID didn't exist.
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM devices WHERE id = ?", (device_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0
