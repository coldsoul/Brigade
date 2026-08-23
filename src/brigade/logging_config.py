"""Logging setup for Brigade."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with a consistent, human-readable format."""
    logging.basicConfig(level=level, format=_FORMAT, datefmt=_DATE_FORMAT)
