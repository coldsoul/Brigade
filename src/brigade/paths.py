"""Shared filesystem helpers for locating a brigade project."""

from __future__ import annotations

from pathlib import Path


def find_brigade_dir(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default cwd) looking for a `.brigade/` directory.

    Returns the path to the `.brigade/` directory if found, or None.
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        brigade_dir = parent / ".brigade"
        if brigade_dir.is_dir():
            return brigade_dir
    return None
