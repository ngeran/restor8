"""Typed PyEZ wrapper — the ONLY module in restor8 that opens NETCONF sessions.

Why this exists (spec §2/§4): connector is the single service allowed to
talk to devices, and this class is its entire device-facing surface. It
buys three things the raw PyEZ API doesn't:

1. **Live feedback** — every step emits a ``DeviceEvent`` through
   ``on_event``, which the gateway later fans out to the browser. The
   event stream is the product requirement, not optional plumbing.
2. **Typed failures** — PyEZ's exception zoo is translated (via
   :func:`restor8_core.models.map_pyez_error`) into restor8 errors that
   keep the Junos message verbatim, so a failed push is debuggable from
   the API response alone.
3. **Safe pushes** — config changes ALWAYS go through
   ``lock → load → diff → commit confirmed``. A confirmed commit is what
   saves you from locking yourself out of a cRPD when a TE config typos a
   loopback: the device reverts on its own if ``confirm_commit()`` never
   arrives. The only permanent commits on this class are
   :meth:`confirm_commit` (explicitly success-gated) and :meth:`rollback`
   (restores the previously-committed known-good config).

Operational-command comments (``# equivalent to: show …``) are attached to
every RPC call — six months from now nobody remembers which RPC maps to
which CLI command.
"""

from __future__ import annotations

import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal
from xml.etree import ElementTree as ET

from jnpr.junos import Device
from jnpr.junos.utils.config import Config

from .events import EventEmitter, OnEvent, Stage
from .models import (
    DeviceFacts,
    DeviceUnreachableError,
    Restor8Error,
    facts_to_model,
    map_pyez_error,
)

ConfigFormat = Literal["text", "set"]
"""Config payload formats. Pick ONE per device and stay consistent — the
backup service diffs these strings, so mixing formats makes Git history
meaningless (decision baked into backup, Phase 2)."""

ConfigMode = Literal["merge", "override", "replace", "update"]
"""How a payload is applied: scenario pushes use ``merge``; full-config
restore uses ``override`` (replace the entire config)."""


class JunosConnection:
    """One NETCONF session to one Junos device, wrapped for restor8.

    Typical use as a context manager (connects and closes itself)::

        with JunosConnection(host, user, pwd, on_event=sink) as jc:
            facts = jc.facts
            diff = jc.push_config(payload, fmt="text", mode="override")
            jc.confirm_commit()   # only after validation passes

    The class is intentionally synchronous: FastAPI runs sync endpoints
    in a threadpool, and PyEZ is blocking end-to-end.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        *,
        port: int = 830,
        timeout: int = 30,
        session_id: str | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        """Args:
            host: management IP/hostname (inventory mgmt_ip, or the
                containerlab node address).
            username: SSH user (from the shared lab credential).
            password: SSH password (shared lab credential today; SSH-key
                auth is a later addition, not wired yet).
            port: NETCONF port (Junos default 830; must be enabled on cRPD).
            timeout: per-RPC timeout in seconds — commits on slow vJunos
                can legitimately take tens of seconds.
            session_id: correlation ID; generated when omitted. The gateway
                keys its WebSocket fan-out on this.
            on_event: sink called at every stage; ``None`` disables events.
        """
        self.host = host
        self.username = username
        self._password = password
        self._port = port
        self._rpc_timeout = timeout
        self._events = EventEmitter(session_id or uuid.uuid4().hex[:12], host, on_event)
        self._dev: Device | None = None
        self._facts: DeviceFacts | None = None

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """Correlation ID stamped on every event of this session."""
        return self._events.session_id

    @property
    def facts(self) -> DeviceFacts:
        """Facts gathered at connect time (valid only while connected)."""
        if self._dev is None or not self._connected():
            raise Restor8Error("not connected — call connect() first", device=self.host)
        assert self._facts is not None  # noqa: S101 — set together with _dev
        return self._facts

    def connect(self) -> DeviceFacts:
        """Open the NETCONF session and gather facts.

        Emits: ``resolving`` (TCP probe with measured latency),
        ``connecting``/``authenticating`` (best-effort granularity — PyEZ
        folds both into ``open()``), ``connected``.

        Returns:
            The device facts (hostname, model, version, …).

        Raises:
            DeviceUnreachableError: TCP probe or SSH/NETCONF open failed.
            AuthenticationFailedError: credentials rejected.
            DeviceRpcTimeoutError: hello/facts RPC timed out.
        """
        with self._guard(Stage.CONNECTING):
            self._events.emit(Stage.RESOLVING, f"probing {self.host}:{self._port}")
            self._probe()
            self._events.emit(Stage.CONNECTING, "opening SSH/NETCONF session")
            self._events.emit(Stage.AUTHENTICATING, f"authenticating as {self.username}")
            dev = Device(
                host=self.host,
                user=self.username,
                password=self._password,
                port=self._port,
                timeout=self._rpc_timeout,
                gather_facts=True,
            )
            dev.open()
            self._dev = dev
            self._facts = facts_to_model(dev.facts)
            label = self._facts.hostname or self.host
            self._events.emit(
                Stage.CONNECTED,
                f"connected to {label} ({self._facts.model or '?'} {self._facts.version or '?'})",
            )
            return self._facts

    def close(self) -> None:
        """Close the session. Never raises — safe in ``finally`` blocks.

        Emits ``closed``. A close failure (device already gone, network
        dropped) is reported as an event, not an exception, so it can't
        mask the original error of an operation.
        """
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception as exc:  # noqa: BLE001 — see docstring
                self._events.emit(Stage.CLOSED, f"close raised (ignored): {exc}")
            else:
                self._events.emit(Stage.CLOSED, "session closed")
            self._dev = None

    def __enter__(self) -> JunosConnection:
        """Context-manager entry: connects and returns self."""
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Context-manager exit: always closes the session."""
        self.close()

    # ── operations ───────────────────────────────────────────────────

    def get_config(self, fmt: ConfigFormat = "text") -> str:
        """Fetch the running configuration as a string.

        Used by the backup service (Phase 2); the format must stay stable
        per device so Git diffs remain meaningful.

        Args:
            fmt: ``text`` (curly-brace hierarchy) or ``set`` (flat
                ``set …`` statements).

        Returns:
            The configuration text.

        Raises:
            DeviceRpcTimeoutError: fetch exceeded the RPC timeout.
            Restor8Error: session closed or transport failed.
        """
        self._require_open()
        with self._guard(Stage.CONNECTED):
            assert self._dev is not None  # noqa: S101 — _require_open narrows
            # equivalent to: show configuration | display set   (fmt="set")
            # equivalent to: show configuration                 (fmt="text")
            rpc = self._dev.rpc.get_config(options={"format": fmt})
            return "".join(rpc.itertext()).strip()

    def rpc(self, name: str, **kwargs: str) -> str:
        """Run an operational RPC by name; return its XML as text.

        The generic read surface for validation snapshots (restore's
        JSNAPy pre/post checks, Phase 5's convergence polling). Kept here
        — not as raw PyEZ passthrough in services — so every device
        interaction stays inside the event-emitting wrapper.

        Args:
            name: Junos RPC identifier, e.g.
                ``get_bgp_summary_information``
                (equivalent to: ``show bgp summary``),
                ``get_interface_information`` (``show interfaces``),
                ``get_mpls_lsp_information`` (``show mpls lsp``).
            **kwargs: RPC arguments as PyEZ builds them (e.g.
                ``terse=True``, ``interface_name="et-0/0/0"``).

        Returns:
            The RPC reply serialised as an XML string (namespaces kept —
            JSNAPy tests match on them).

        Raises:
            Restor8Error: unknown RPC name (AttributeError mapped) or
                transport failure.
            DeviceRpcTimeoutError: RPC exceeded the timeout.
        """
        self._require_open()
        with self._guard(Stage.CONNECTED):
            assert self._dev is not None  # noqa: S101 — _require_open narrows
            try:
                fn = getattr(self._dev.rpc, name)
            except AttributeError as exc:
                raise Restor8Error(
                    f"unknown RPC '{name}'", device=self.host
                ) from exc
            # equivalent to: the `show …` command matching `name`
            result = fn(**kwargs)
            return ET.tostring(result, encoding="unicode")

    def push_config(
        self,
        payload: str,
        *,
        fmt: ConfigFormat = "text",
        mode: ConfigMode = "merge",
        confirm_minutes: int = 2,
        comment: str = "restor8",
    ) -> str:
        """Load a config payload with a confirmed commit — the ONLY push path.

        Pipeline (each step emits an event):
            ``locking`` → ``loading-config`` → ``diff-ready`` →
            ``committing`` → ``commit-confirmed`` → ``unlocking``.

        The commit is CONFIRMED: unless :meth:`confirm_commit` is called
        within ``confirm_minutes``, the device reverts to the previous
        config on its own. A caller that just pushed garbage loses nothing
        but the wait.

        Args:
            payload: config text in ``fmt`` form.
            fmt: payload format (``text`` or ``set``).
            mode: ``merge`` for additive scenario pushes, ``override``
                for whole-config restore.
            confirm_minutes: confirmed-commit window.
            comment: Junos commit comment (shows in ``show system commit``).

        Returns:
            The pending diff (equivalent to ``show | compare``) — empty
            string when the payload changes nothing.

        Raises:
            LockFailedError: someone else holds the candidate lock.
            LoadFailedError: payload rejected by Junos syntax checks.
            CommitFailedError: commit rejected — message carries the
                Junos ``error: …`` text verbatim.
        """
        self._require_open()
        assert self._dev is not None  # noqa: S101 — _require_open narrows
        cu = Config(self._dev)
        locked = False
        try:
            with self._guard(Stage.LOCKING):
                # equivalent to: configure exclusive
                self._events.emit(Stage.LOCKING, "acquiring candidate lock")
                cu.lock()
                locked = True

            with self._guard(Stage.LOADING_CONFIG):
                # equivalent to: load merge terminal / load override terminal
                self._events.emit(
                    Stage.LOADING_CONFIG,
                    f"loading {len(payload)}B of {fmt} config ({mode})",
                )
                cu.load(payload, format=fmt, mode=mode)

            diff = ""
            with self._guard(Stage.DIFF_READY):
                # equivalent to: show | compare  (pending candidate diff)
                diff = cu.diff() or ""
                self._events.emit(
                    Stage.DIFF_READY,
                    f"pending diff ready ({len(diff.splitlines())} lines)",
                    diff=diff,
                )

            with self._guard(Stage.COMMITTING):
                # equivalent to: commit confirmed <minutes> comment "<comment>"
                self._events.emit(
                    Stage.COMMITTING, f"issuing commit confirmed {confirm_minutes}m"
                )
                cu.commit(confirm=confirm_minutes, comment=comment)
                self._events.emit(
                    Stage.COMMIT_CONFIRMED,
                    f"device auto-reverts in {confirm_minutes}m unless confirmed",
                    confirm_minutes=confirm_minutes,
                )
            return diff
        except Exception:
            # Never leave a candidate lock (or a half-loaded candidate)
            # behind on failure — best-effort discard + unlock, then let
            # the original typed error propagate from _guard.
            self._discard_candidate(cu, locked=locked)
            raise
        finally:
            if locked:
                with self._guard(Stage.UNLOCKING):
                    # equivalent to: exiting configuration mode (releases lock)
                    self._events.emit(Stage.UNLOCKING, "releasing candidate lock")
                    cu.unlock()

    def confirm_commit(self, comment: str = "restor8: confirmed good") -> None:
        """Finalise a confirmed commit before its window expires.

        Call this only once validation (JSNAPy post-check, Phase 3/5)
        passes — it makes the candidate the permanent running config.

        Raises:
            CommitFailedError: the confirming commit failed.
        """
        self._require_open()
        assert self._dev is not None  # noqa: S101 — _require_open narrows
        cu = Config(self._dev)
        with self._guard(Stage.COMMITTING):
            # equivalent to: commit  (finalises the pending confirmed commit)
            cu.commit(comment=comment)
            self._events.emit(
                Stage.COMMITTING, "commit confirmed (permanent)", permanent=True
            )

    def rollback(self) -> str:
        """Roll the device back to the previously committed config.

        Used by restore (Phase 3) when a post-push JSNAPy check fails.
        This is a PERMANENT commit — deliberately: every restor8 push is
        itself confirmed-commit gated, so the previous commit is by
        definition the known-good config; re-confirming it would only
        re-expose the device to the failure we're escaping.

        Returns:
            The diff of the rollback (old ← new), for the audit trail.

        Raises:
            LockFailedError / CommitFailedError: as for :meth:`push_config`.
        """
        self._require_open()
        assert self._dev is not None  # noqa: S101 — _require_open narrows
        cu = Config(self._dev)
        locked = False
        try:
            with self._guard(Stage.LOCKING):
                cu.lock()
                locked = True
            with self._guard(Stage.ROLLING_BACK):
                # equivalent to: rollback 0  (discard back to last committed)
                cu.rollback(rb=0)
                diff = cu.diff() or ""
                self._events.emit(Stage.ROLLING_BACK, "rollback 0 loaded", diff=diff)
                # equivalent to: commit comment "restor8: rollback"
                cu.commit(comment="restor8: rollback after failed validation")
                self._events.emit(
                    Stage.COMMIT_CONFIRMED, "rollback committed (permanent)"
                )
            return diff
        except Exception:
            self._discard_candidate(cu, locked=locked)
            raise
        finally:
            if locked:
                with self._guard(Stage.UNLOCKING):
                    cu.unlock()

    # ── internals ────────────────────────────────────────────────────

    def _probe(self) -> float:
        """TCP-probe host:port before PyEZ gets involved.

        A refused/filtered port-830 reads as "NETCONF not enabled" here,
        which is far more actionable than PyEZ's generic ConnectError.

        Returns:
            Round-trip latency in milliseconds (attached to the event).

        Raises:
            DeviceUnreachableError: connect refused/timeout/DNS failure.
        """
        t0 = time.monotonic()
        try:
            with socket.create_connection(
                (self.host, self._port), timeout=min(self._rpc_timeout, 10)
            ):
                pass
        except OSError as exc:
            err = DeviceUnreachableError(
                f"TCP probe {self.host}:{self._port} failed: {exc}",
                device=self.host,
                stage=Stage.RESOLVING.value,
            )
            self._events.emit(Stage.ERROR, err.args[0], error=type(exc).__name__)
            raise err from exc
        latency_ms = (time.monotonic() - t0) * 1000
        self._events.emit(
            Stage.RESOLVING, "device reachable", latency_ms=round(latency_ms, 1)
        )
        return latency_ms

    @contextmanager
    def _guard(self, stage: Stage) -> Iterator[None]:
        """Translate + report any exception raised inside the block.

        Emits exactly one ``error`` event per failure (already-typed
        restor8 errors pass through untouched) and re-raises the typed
        error with the original as ``__cause__``.
        """
        try:
            yield
        except Restor8Error as exc:
            if exc.device == "":
                exc.device = self.host
            raise
        except Exception as exc:
            mapped = map_pyez_error(exc, device=self.host, stage=stage.value)
            self._events.emit(
                Stage.ERROR,
                f"{mapped.__class__.__name__}: {exc}",
                error=mapped.__class__.__name__,
                failed_stage=stage.value,
            )
            raise mapped from exc

    def _discard_candidate(self, cu: Config, *, locked: bool) -> None:
        """Best-effort ``rollback 0`` + unlock after a failed push.

        Failures here are swallowed and only evented — they must not mask
        the original error that triggered recovery.
        """
        try:
            if locked:
                cu.rollback(rb=0)  # equivalent to: rollback 0 (discard candidate)
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            self._events.emit(Stage.ERROR, f"discard-candidate failed: {exc}")
        finally:
            try:
                if locked:
                    cu.unlock()
            except Exception as exc:  # noqa: BLE001
                self._events.emit(Stage.ERROR, f"unlock during recovery failed: {exc}")

    def _require_open(self) -> None:
        """Raise a typed error if the session isn't open."""
        if self._dev is None or not self._connected():
            raise Restor8Error(
                "not connected — call connect() first", device=self.host
            )

    def _connected(self) -> bool:
        """True when the underlying PyEZ session is open."""
        return bool(self._dev and self._dev.connected)
