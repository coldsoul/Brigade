"""Logging setup for Brigade."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
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
    level: int = logging.INFO, log_dir: Path | None = None, console: bool = True
) -> None:
    """Configure the root logger with a consistent, human-readable format.

    Logs go to the console by default; when *log_dir* is given they are also
    written to `log_dir / "brigade.log"` with rotation.  Pass `console=False`
    when a TUI owns the terminal (so log lines don't corrupt the display).
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    silence_litellm()

    formatter = CorrelationFormatter(_FORMAT, datefmt=_DATE_FORMAT)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "brigade.log", maxBytes=1_000_000, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level)


def silence_litellm() -> None:
    """Completely disable litellm's own logging.

    litellm attaches StreamHandlers to several loggers at import time and logs
    "LiteLLM completion() …" at INFO level.  Since litellm is imported lazily,
    this is called both at configure time and after the first `import litellm`.
    It detaches the handlers, stops propagation, and raises the level so that no
    litellm record of any severity is emitted anywhere.
    """
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm"):
        logger = logging.getLogger(name)
        logger.disabled = True  # fully inert — no records, no lastResort fallback
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)


@contextlib.contextmanager
def redirect_fds_to_file(path: Path):
    """Redirect the process's real stdout/stderr file descriptors to *path*.

    Unlike `contextlib.redirect_stdout`, this operates at the OS file-descriptor
    level (via `os.dup2`), so it catches writes from C extensions and any
    dependency that writes directly to fd 1/2 rather than through Python's
    `sys.stdout`/`logging` — which is exactly what's needed while a Textual app
    owns the terminal. Restores the original fds on exit, including on an
    unhandled exception.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)

    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    saved_stdout_fd = os.dup(stdout_fd)
    saved_stderr_fd = os.dup(stderr_fd)

    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log_fd, stdout_fd)
        os.dup2(log_fd, stderr_fd)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout_fd, stdout_fd)
        os.dup2(saved_stderr_fd, stderr_fd)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        os.close(log_fd)
