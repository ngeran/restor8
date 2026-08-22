"""inventory — the registry of lab devices (Phase 1).

Every other service answers "which devices exist?" through this API:
backup iterates it, restore targets entries in it, scenario selects node
subsets from it, the topology service reconciles containerlab nodes
against it. It is pure CRUD over SQLite — device *reachability* is
connector's concern, so this service holds no credentials and opens no
sessions (its Deployment has no lab-auth env at all).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from db import InventoryDB
from restor8_core.jsonlog import setup_logging

# JSON lines from the first log call on (no root logging config existed).
setup_logging("inventory")

app = FastAPI(
    title="restor8 inventory",
    description="Registry of lab devices: what exists and how to address it.",
    version="0.1.0",
)

db = InventoryDB()


class DeviceIn(BaseModel):
    """Payload to register a device.

    ``mgmt_ip``/``port`` are the address **as reachable from inside the
    cluster** (containerlab nodes: node IP + published port).
    """

    name: str = Field(description="device hostname, e.g. P-1", min_length=1)
    mgmt_ip: str = Field(description="cluster-reachable mgmt address")
    port: int = Field(default=830, description="NETCONF/SSH port")
    platform: str = Field(default="", description="cRPD | vJunos-router | MX | ACX …")
    auth_ref: str = Field(
        default="lab-auth",
        description="k8s Secret name holding this device's credential "
        "(default = the shared lab credential)",
    )
    containerlab_node: str | None = Field(
        default=None, description="containerlab node name once topology links it"
    )


class DeviceUpdate(BaseModel):
    """Partial update — only the fields present are changed."""

    name: str | None = None
    mgmt_ip: str | None = None
    port: int | None = None
    platform: str | None = None
    auth_ref: str | None = None
    containerlab_node: str | None = None


class Device(DeviceIn):
    """A registered device (what the API returns)."""

    id: int
    created_at: str


@app.get("/")
def index() -> dict[str, str]:
    """Service banner — also the smoke-test 200 target."""
    return {"service": "restor8-inventory", "status": "running"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness/readiness probe target."""
    return {"status": "ok"}


@app.get("/devices", response_model=list[Device])
def list_devices() -> list[dict[str, Any]]:
    """List every registered device."""
    return db.list_devices()


@app.get("/devices/{device_id}", response_model=Device)
def get_device(device_id: int) -> dict[str, Any]:
    """Fetch one device by ID.

    Raises:
        HTTPException 404: unknown ID.
    """
    row = db.get_device(device_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"device {device_id} not found")
    return row


@app.post("/devices", response_model=Device, status_code=201)
def create_device(dev: DeviceIn) -> dict[str, Any]:
    """Register a device.

    Raises:
        HTTPException 409: name already registered.
    """
    try:
        return db.create_device(dev.model_dump())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"device '{dev.name}' exists") from exc


@app.patch("/devices/{device_id}", response_model=Device)
def update_device(device_id: int, patch: DeviceUpdate) -> dict[str, Any]:
    """Patch selected fields of a device.

    Raises:
        HTTPException 400: no fields to update.
        HTTPException 404: unknown ID.
        HTTPException 409: the new name collides with another device.
    """
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        row = db.update_device(device_id, fields)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="name already in use") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"device {device_id} not found")
    return row


@app.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int) -> None:
    """Remove a device.

    Raises:
        HTTPException 404: unknown ID.
    """
    if not db.delete_device(device_id):
        raise HTTPException(status_code=404, detail=f"device {device_id} not found")
