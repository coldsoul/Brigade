"""Logging setup for Brigade."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s%(correlation)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


class CorrelationFormatter(logging.Formatter):
    """Formatter that renders an optional `behaviour_id` correlation field."""

    def format(self, record: logging.LogRecord) -> str:
        bid = getattr(record, "behaviour_id", None)
        record.correlation = f" bid={bid}" if bid else ""
        return super().format(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a consistent, human-readable format."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(CorrelationFormatter(_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
