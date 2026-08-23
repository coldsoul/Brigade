"""Logging setup for Brigade."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s%(correlation)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


class CorrelationFormatter(logging.Formatter):
    """Formatter that renders an optional `behaviour_id` correlation field."""

    def format(self, record: logging.LogRecord) -> str:
        bid = getattr(record, "behaviour_id", None)
        record.correlation = f" bid={bid}" if bid else ""
        return super().format(record)


def configure_logging(
    level: int = logging.INFO, log_dir: Path | None = None
) -> None:
    """Configure the root logger with a consistent, human-readable format.

    Logs always go to the console; when *log_dir* is given, they are also
    written to `log_dir / "brigade.log"` with rotation.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    formatter = CorrelationFormatter(_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "brigade.log", maxBytes=1_000_000, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level)
