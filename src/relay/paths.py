"""Shared filesystem helpers for locating a relay project."""

from __future__ import annotations

from pathlib import Path


def find_relay_dir(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default cwd) looking for a `.relay/` directory.

    Returns the path to the `.relay/` directory if found, or None.
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in [cwd, *cwd.parents]:
        relay_dir = parent / ".relay"
        if relay_dir.is_dir():
            return relay_dir
    return None
