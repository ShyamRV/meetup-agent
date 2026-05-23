"""Structured JSON logging for all agents.

Every agent in the monorepo emits one JSON object per log line so the
shipping log pipeline (Vector → Loki / CloudWatch) can index fields
without regex parsing. Use :func:`get_logger` once per module.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, ClassVar


class JsonFormatter(logging.Formatter):
    """Render every log record as a one-line JSON object."""

    _RESERVED: ClassVar[set[str]] = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # ``ensure_ascii=True`` keeps every log line printable on any
        # downstream stream regardless of locale — Windows cp1252 consoles
        # in particular choke on raw UTF-8. Unicode chars become ``\uXXXX``
        # which the log pipeline parses back identically.
        return json.dumps(payload, ensure_ascii=True, default=str)


_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    _CONFIGURED = True


class StructuredLogger:
    """Thin adapter that lets call sites pass arbitrary kwargs as JSON fields.

    Wrapping (rather than subclassing) keeps us out of the way of any
    other code in-process that grabs ``logging.getLogger(name)`` directly.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _emit(self, level: int, msg: str, kwargs: dict[str, Any]) -> None:
        if not self._logger.isEnabledFor(level):
            return
        exc_info = kwargs.pop("exc_info", None)
        stack_info = kwargs.pop("stack_info", False)
        extra = kwargs.pop("extra", None) or {}
        extra.update(kwargs)
        self._logger.log(
            level,
            msg,
            exc_info=exc_info,
            stack_info=stack_info,
            extra=extra,
        )

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.DEBUG, msg, kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.INFO, msg, kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.WARNING, msg, kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._emit(logging.ERROR, msg, kwargs)

    def exception(self, msg: str, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, kwargs)

    @property
    def stdlib(self) -> logging.Logger:
        """Escape hatch when a stdlib logger is required (e.g. third-party APIs)."""

        return self._logger


def get_logger(name: str) -> StructuredLogger:
    """Return a structured logger wrapper that writes JSON lines."""

    _configure_root()
    return StructuredLogger(logging.getLogger(name))
