"""JSNAPy adapter — file-based pre/post validation, no device sessions.

Why this exists (spec §4): every restore and scenario run validates
pre/post state with JSNAPy. But restor8's architecture rule is that only
connector opens device sessions — so this runner never connects to
anything: snapshots arrive as XML strings (connector's ``/snapshot``),
get written to tempfiles, and JSNAPy compares them offline.

Encapsulated quirks (verified empirically against jsnapy 1.3.8 — don't
re-derive them):

* JSNAPy demands a home directory containing ``logging.yml`` +
  ``jsnapy.cfg`` at *import/instantiation* time; the container's root fs
  is read-only, so we point ``JSNAPY_HOME`` (checked first by jsnapy)
  at a writable dir and materialise both files.
* The stock ``logging.yml`` sets ``disable_existing_loggers: True`` —
  importing jsnapy would silence restor8's own loggers (events!). Ours
  keeps them.
* ``SnapAdmin.check`` takes a main-config FILE whose ``tests`` entries
  are NAMES of test files resolved against ``test_file_path`` from
  ``jsnapy.cfg`` — inline test definitions and dict configs both crash
  (the crash is hidden behind jsnapy's own %-format bug at
  jsnapy.py:795). Hence the two-file dance below.
* The main config still needs a ``hosts`` section even in file-based
  mode (no connection is made when pre/post files are given).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_LOGGING_YML = """\
version: 1
# NEVER True here — jsnapy's stock file sets it and would silence every
# already-configured restor8 logger (the event stream!) on import.
disable_existing_loggers: false
formatters:
  default:
    format: "%(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: default
root:
  level: WARNING
  handlers: [console]
"""


def ensure_jsnapy_home() -> Path:
    """Create (once) the writable JSNAPY home with its config files.

    Returns:
        The home path (``$JSNAPY_HOME`` or ``/tmp/jsnapy``).
    """
    home = Path(os.environ.setdefault("JSNAPY_HOME", "/tmp/jsnapy"))
    (home / "testfiles").mkdir(parents=True, exist_ok=True)
    (home / "snapshots").mkdir(parents=True, exist_ok=True)
    if not (home / "logging.yml").exists():
        (home / "logging.yml").write_text(_LOGGING_YML)
    if not (home / "jsnapy.cfg").exists():
        (home / "jsnapy.cfg").write_text(
            f"[DEFAULT]\n"
            f"config_file_path = {home}\n"
            f"test_file_path = {home}/testfiles\n"
            f"snapshot_path = {home}/snapshots\n"
        )
    return home


@dataclass
class ComparisonResult:
    """Outcome of one JSNAPy pre/post comparison."""

    passed: bool
    results: list[dict[str, str]] = field(default_factory=list)
    """Per-test entries: {"test": name, "result": "Passed"|"Failed"}."""


def compare(testdef: dict[str, object], pre_xml: str, post_xml: str) -> ComparisonResult:
    """Compare two snapshots with a JSNAPy test definition.

    Args:
        testdef: a JSNAPy *test-file* mapping, e.g.::

            {"test_bgp_established": [
                {"rpc": "get-bgp-summary-information"},
                {"iterate": {"xpath": "//bgp-peer",
                             "tests": [{"is-equal": "peer-state, Established"}]}},
            ]}

        pre_xml: snapshot XML before the change (connector /snapshot).
        post_xml: snapshot XML after the change.

    Returns:
        ComparisonResult with an overall pass flag and per-test results.

    Raises:
        RuntimeError: jsnapy itself failed to run (bad testdef, etc.) —
            distinct from "tests ran and failed".
    """
    ensure_jsnapy_home()
    import yaml
    from jnpr.jsnapy import SnapAdmin

    test_names = list(testdef.keys())
    tag = uuid.uuid4().hex[:8]
    home = Path(os.environ["JSNAPY_HOME"])

    test_file = home / "testfiles" / f"restor8_{tag}.yml"
    test_file.write_text(yaml.dump(testdef))

    # The main config: hosts are mandatory even though no connection is
    # made in file-based mode; tests reference the file by NAME.
    main_cfg = home / f"restor8_main_{tag}.yml"
    main_cfg.write_text(
        yaml.dump(
            {
                "hosts": [{"device": "restor8", "username": "na", "passwd": "na"}],
                "tests": [test_file.name],
            }
        )
    )

    tmp = Path(tempfile.mkdtemp(prefix="restor8_jsnapy_"))
    pre_path = tmp / "pre.xml"
    post_path = tmp / "post.xml"
    pre_path.write_text(pre_xml)
    post_path.write_text(post_xml)

    try:
        raw = SnapAdmin().check(str(main_cfg), pre_file=str(pre_path), post_file=str(post_path))
    except TypeError as exc:
        # jsnapy.py:795 masks real errors with its own %-format bug —
        # surface something actionable instead.
        raise RuntimeError(f"jsnapy failed to run testdef: {exc}") from exc
    finally:
        for p in (test_file, main_cfg, pre_path, post_path):
            p.unlink(missing_ok=True)
        tmp.rmdir()

    items = raw if isinstance(raw, list) else [raw]
    entries: list[dict[str, str]] = []
    for i, item in enumerate(items):
        result = getattr(item, "result", None) or "Unknown"
        entries.append(
            {"test": test_names[i] if i < len(test_names) else f"test_{i}", "result": str(result)}
        )
    return ComparisonResult(passed=all(e["result"] == "Passed" for e in entries), results=entries)
