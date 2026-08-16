"""Shared pydantic models + the typed device-error taxonomy.

Why the typed errors exist: PyEZ raises a dozen exception classes and
 burying them means an 11pm BGP failure debugged with print statements.
Every PyEZ exception is translated here (see :func:`map_pyez_error`) into
one restor8 error carrying the original message verbatim, so the connector
API and the UI can show *what failed on which device at which stage*
without the caller importing anything Juniper-flavoured.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class DeviceFacts(BaseModel):
    """The subset of PyEZ facts restor8 cares about (Phase 0 checkpoint
    proves these come back real: model, version, hostname).

    ``extra="allow"`` keeps unknown-but-interesting facts (cRPD and
    vJunos-router fill different subsets) instead of silently dropping
    them; the UI renders whatever the device actually reported.
    """

    model_config = ConfigDict(extra="allow")

    hostname: str | None = None
    model: str | None = None
    version: str | None = None
    """Junos version string, e.g. ``23.4R1.9`` (cRPD: e.g. ``23.4R1.9-EVO``)."""

    serialnumber: str | None = None
    personality: str | None = None
    """``Router``/``Switch`` — distinguishes MX/ACX-style from cRPD facts."""

    virtual: bool | None = None
    """True for containerised control planes (cRPD, vJunos-router)."""


# ── typed error taxonomy ──────────────────────────────────────────────


class Restor8Error(Exception):
    """Base class for every restor8 device error.

    Attributes:
        device: host the operation targeted.
        stage: pipeline stage that failed ("" if unknown).
    """

    def __init__(self, message: str, *, device: str = "", stage: str = "") -> None:
        """Args:
            message: the underlying failure text, kept verbatim.
            device: target host, for error routing in multi-node scenarios.
            stage: ``Stage`` value name where the failure occurred.
        """
        super().__init__(message)
        self.device = device
        self.stage = stage


class DeviceUnreachableError(Restor8Error):
    """TCP/SSH never established — wrong mgmt IP, NETCONF not enabled,
    port 830 filtered (PyEZ ``ConnectError``/``ConnectRefusedError``/
    ``ConnectTimeoutError``/``ConnectUnknownHostError``)."""


class AuthenticationFailedError(Restor8Error):
    """Credentials rejected (PyEZ ``ConnectAuthError``)."""


class LockFailedError(Restor8Error):
    """Candidate config lock refused — another session holds it
    (PyEZ ``LockError``/``UnlockError``); equivalent to a competing
    ``configure exclusive``."""


class CommitFailedError(Restor8Error):
    """Commit rejected by the device (PyEZ ``CommitError``) — usually a
    syntax/semantic config error; the message contains the Junos error
    text (``error: …``)."""


class LoadFailedError(Restor8Error):
    """Config payload rejected at load time (PyEZ ``ConfigLoadError``)."""


class DeviceRpcTimeoutError(Restor8Error):
    """RPC exceeded its timeout (PyEZ ``RpcTimeoutError``) — common when
    a commit takes longer than expected on a slow vMX/vJunos."""


def map_pyez_error(exc: Exception, *, device: str = "", stage: str = "") -> Restor8Error:
    """Translate a PyEZ exception into the restor8 taxonomy.

    Args:
        exc: the raised exception (PyEZ or anything else).
        device: host the operation targeted.
        stage: pipeline stage name where it was raised.

    Returns:
        A typed ``Restor8Error`` subclass whose message is ``str(exc)``
        verbatim — never summarise away the Junos error text.

    Raises:
        Nothing — always returns; unmapped exceptions become a plain
        ``Restor8Error`` preserving type name and message.
    """
    # Imported lazily so importing this module never drags PyEZ's
    # (optional-at-runtime) transitive deps into e.g. the gateway.
    from jnpr.junos import exception as pz

    message = f"{exc}" if str(exc) else exc.__class__.__name__

    if isinstance(exc, pz.ConnectAuthError):
        cls: type[Restor8Error] = AuthenticationFailedError
    elif isinstance(exc, pz.ConnectError):  # parent of refused/timeout/unknown-host
        cls = DeviceUnreachableError
    elif isinstance(exc, (pz.LockError, pz.UnlockError)):
        cls = LockFailedError
    elif isinstance(exc, pz.CommitError):
        cls = CommitFailedError
    elif isinstance(exc, pz.ConfigLoadError):
        cls = LoadFailedError
    elif isinstance(exc, pz.RpcTimeoutError):
        cls = DeviceRpcTimeoutError
    else:
        cls = Restor8Error
        message = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__

    return cls(message, device=device, stage=stage)


def _jsonable(value: Any) -> Any:
    """Coerce an arbitrary fact value into something JSON-safe.

    PyEZ facts contain non-primitive objects (e.g. ``version_info`` is a
    ``jnpr.junos.facts.swver.version_info`` instance); ``model_dump`` in
    JSON mode raises on those. Primitives pass, containers recurse,
    everything else becomes its ``str()`` — losing nothing the UI would
    have rendered anyway.

    Args:
        value: a single fact value.

    Returns:
        The JSON-safe equivalent of ``value``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def facts_to_model(facts: Any) -> DeviceFacts:
    """Coerce a PyEZ facts mapping into :class:`DeviceFacts`.

    Args:
        facts: any mapping (``Device.facts``, dict) — unknown keys are
            retained via ``extra="allow"`` and sanitized to JSON-safe
            values (see :func:`_jsonable`).

    Returns:
        The validated model; missing keys default to ``None``.
    """
    return DeviceFacts.model_validate(
        {k: _jsonable(v) for k, v in dict(facts).items()}
    )
