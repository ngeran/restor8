"""Progress-event schema — the backbone of restor8's live-feedback UX.

Why this module exists: the core product requirement is "real-time
feedback from the device" — seeing *connecting → locked → committing →
commit-confirmed* tick by tick in the browser while a scenario runs.
That only stays coherent if there is exactly one event schema and exactly
one producer: `restor8_core.junos.JunosConnection` emits `DeviceEvent`s
through its ``on_event`` callback, connector owns the sessions (spec §2),
and the gateway fans the same objects out over WebSocket (Phase 6).
Defining the schema here — in the shared lib — means producer and
consumers cannot drift.
"""

from __future__ import annotations

import enum
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field


class Stage(str, enum.Enum):
    """Coarse progress stages of a device session.

    Listed roughly in occurrence order for a config push; ``ERROR`` can
    arrive at any point and terminates the operation.
    """

    RESOLVING = "resolving"                # TCP probe of host:port before NETCONF
    CONNECTING = "connecting"              # opening the SSH/NETCONF session
    AUTHENTICATING = "authenticating"      # credentials being accepted
    CONNECTED = "connected"                # session up, facts available
    LOCKING = "locking"                    # candidate config lock (configure exclusive)
    LOADING_CONFIG = "loading-config"      # loading payload into the candidate
    DIFF_READY = "diff-ready"              # pending diff returned, pre-commit
    COMMITTING = "committing"              # commit confirmed issued
    COMMIT_CONFIRMED = "commit-confirmed"  # device will auto-rollback unless confirmed
    ROLLING_BACK = "rolling-back"          # explicit rollback in progress
    UNLOCKING = "unlocking"                # releasing the candidate lock
    CLOSED = "closed"                      # session torn down cleanly
    ERROR = "error"                        # operation failed; see message/detail


class DeviceEvent(BaseModel):
    """One progress step of a device operation.

    Designed to be shipped verbatim over WebSocket as JSON — the gateway
    adds routing (run/session IDs), never rewrites content.
    """

    session_id: str
    """Groups all events of one logical operation (connect, push, backup…)."""

    device: str
    """Target host or inventory name — lets the UI fan out per-node tiles."""

    stage: Stage
    message: str
    """Human-readable one-liner for the timeline; keep it UI-printable."""

    detail: dict[str, Any] = Field(default_factory=dict)
    """Optional structured payload (diff text, latency, error class…)."""

    ts: float = Field(default_factory=time.time)
    """Unix epoch seconds, set at emit time."""


OnEvent = Callable[[DeviceEvent], None]
"""Signature of the sink ``JunosConnection`` calls at every stage."""


class EventEmitter:
    """Stamps and forwards events for one (session, device) pair.

    A sink failure must NEVER abort a device operation mid-commit — a dead
    WebSocket client should not leave a cRPD holding a candidate lock —
    so ``emit`` swallows sink exceptions entirely.
    """

    def __init__(
        self,
        session_id: str,
        device: str,
        sink: OnEvent | None,
    ) -> None:
        """Args:
            session_id: correlation ID for the whole operation.
            device: host or inventory name the events are about.
            sink: callback invoked per event; ``None`` disables emission
                (useful for tests that only care about return values).
        """
        self._session_id = session_id
        self._device = device
        self._sink = sink

    @property
    def session_id(self) -> str:
        """The correlation ID this emitter stamps onto every event."""
        return self._session_id

    def emit(self, stage: Stage, message: str, **detail: Any) -> None:
        """Forward one event to the sink, ignoring sink errors.

        Args:
            stage: pipeline step that just completed/started.
            message: human-readable timeline text.
            **detail: structured extras attached to the event.
        """
        if self._sink is None:
            return
        try:
            self._sink(
                DeviceEvent(
                    session_id=self._session_id,
                    device=self._device,
                    stage=stage,
                    message=message,
                    detail=detail,
                )
            )
        except Exception:
            # Deliberately swallowed — see class docstring.
            pass


# ── gateway relay ──────────────────────────────────────────────────────
#
# Services that want their events in the browser feed set GATEWAY_URL;
# `relay_sink` wraps their local logging sink with a fire-and-forget POST
# to the gateway's /internal/events. Delivery is best-effort BY DESIGN:
# a down gateway must never delay a device operation (same rule as the
# EventEmitter's swallow-all).

_RELAY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay")
_RELAY_LOG = logging.getLogger("restor8.relay")


def relay_sink(local: OnEvent) -> OnEvent:
    """Wrap a local sink with best-effort forwarding to the gateway.

    Args:
        local: the service's own sink (JSON log line, typically).

    Returns:
        A sink that calls ``local`` synchronously and POSTs the event to
        ``$GATEWAY_URL/internal/events`` on a worker thread when the env
        var is set.
    """

    gateway = os.environ.get("GATEWAY_URL", "").rstrip("/")

    def _sink(event: DeviceEvent) -> None:
        local(event)
        if not gateway:
            return

        def _post() -> None:
            # Imported lazily: not every service's venv carries httpx, and
            # this module (events) is imported by all of them.
            import httpx

            try:
                httpx.post(
                    f"{gateway}/internal/events",
                    json=event.model_dump(mode="json"),
                    timeout=5,
                )
            except Exception:  # noqa: BLE001 — relay is best-effort
                _RELAY_LOG.debug("relay failed", exc_info=True)

        _RELAY_POOL.submit(_post)

    return _sink
