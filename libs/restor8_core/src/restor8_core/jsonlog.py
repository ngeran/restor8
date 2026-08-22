"""Structured JSON logging — one JSON object per line, stdlib only.

Why JSON lines and not a formatter lib: promtail ships container stdout
to Loki verbatim, so line-delimited JSON is the *entire* observability
integration — no sidecar, no parser config; Grafana's ``| json`` does
the rest. Every ``extra`` key a logger passes (``device``, ``event``,
``session_id``, …) merges at the top level, which is what makes
``{namespace="restor8"} | json | event="commit-confirmed"`` a query.

Usage (each service's ``main.py``):

    from restor8_core.jsonlog import setup_logging

    setup_logging("gateway")   # replaces logging.basicConfig

Loggers named ``restor8.<service>`` report their short name; anything
else (uvicorn, httpx) is tagged with the service passed in, so lines
from a pod are always attributable to exactly one service.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

# LogRecord's own attributes — everything else in __dict__ came in via
# ``extra=`` and belongs in the JSON object as a first-class field.
_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class _JsonFormatter(logging.Formatter):
    """Minimal stdlib JSON-lines formatter (ts / level / service / msg + extras)."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        name = (
            record.name.removeprefix("restor8.")
            if record.name.startswith("restor8.")
            else self._service
        )
        entry: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "service": name,
            "msg": record.getMessage(),
        }
        entry.update(
            {k: v for k, v in record.__dict__.items() if k not in _RECORD_ATTRS}
        )
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # default=str: facts/dicts occasionally ride along in extras —
        # degrade to their str() instead of killing the log line.
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(service: str, level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger (replaces basicConfig).

    Idempotent: re-calling (tests, --reload) replaces the handler instead
    of stacking a second one and double-printing every line.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
