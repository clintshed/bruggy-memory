"""Structured JSON logging, per the homelab logging design.

Output is one JSON object per line::

    {"ts":"2024-01-01T00:00:00Z","level":"INFO","logger":"ingest","msg":"...","extra":{...}}

Rotation is handled by :mod:`logging.handlers.RotatingFileHandler`. A plain
dict ``extra={...}`` on any log call is merged into the emitted JSON so callers
can attach provenance/metrics without fighting the formatter. ``structlog`` is
optional; if present we still emit the same line format so downstream parsing
is consistent whether or not structlog is installed.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import Settings

_FORMAT_CHOICES = ("json", "console")
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc)
        payload = {
            "ts": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if v is not None})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = getattr(record, "extra", None)
        suffix = ""
        if isinstance(extra, dict) and extra:
            suffix = " " + json.dumps(extra, default=str, ensure_ascii=False)
        return f"{record.levelname:<7} {record.getMessage()}{suffix}"


def _resolve_level(level: str) -> int:
    return _LEVELS.index(str(level).upper()) if str(level).upper() in _LEVELS else 20


def configure_logging(
    level: str = "INFO",
    log_dir: str = "./logs",
    rotation: str = "20MB",
    retention: int = 14,
    log_format: str = "json",
    *,
    to_stdout: bool = True,
    to_file: bool = True,
) -> logging.Logger:
    """Configure and return the root ``brussy_memory`` logger.

    Idempotent: calling it more than once updates the existing handlers' level
    and format rather than stacking duplicate handlers.
    """
    level = (level or "INFO").upper()
    log_format = (log_format or "json").lower()
    if log_format not in _FORMAT_CHOICES:
        log_format = "json"

    logger = logging.getLogger("brussy_memory")
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    fmt = _JSONFormatter() if log_format == "json" else _ConsoleFormatter()
    handlers_to_remove = [
        h
        for h in logger.handlers
        if not isinstance(h, logging.StreamHandler) or True
    ]
    # Keep it simple: rebuild handlers each call (cheap, idempotent in practice).
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    root_name = _rotation_bytes(rotation)

    if to_file:
        out = Path(log_dir)
        out.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            out / "brussy-memory.log",
            maxBytes=int(root_name),
            backupCount=int(retention),
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    if to_stdout:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


def _rotation_bytes(rotation: str) -> int:
    """Parse a rotation string like ``20MB`` into a byte count."""
    text = str(rotation).strip().upper()
    if not text:
        return 20 * 1024 * 1024
    units = {"KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024, "B": 1}
    for suffix, mult in units.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * mult)
            except ValueError:
                break
    try:
        return int(text)
    except ValueError:
        return 20 * 1024 * 1024


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under the ``brussy_memory`` tree."""
    return logging.getLogger(f"brussy_memory.{name}")


def log_event(
    logger: logging.Logger,
    level: str,
    event: str,
    **extra: Any,
) -> None:
    """Emit a structured event line with ``event`` plus any extra fields."""
    call = getattr(logger, level.lower(), logger.info)
    call(event, extra={**extra, "event": event})
