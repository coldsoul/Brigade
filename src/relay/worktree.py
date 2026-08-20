"""Git worktree isolation per behaviour.

Each behaviour gets its own worktree at `.relay/work/<behaviour_id>/` on a
branch `relay/<behaviour_id>`.  The worktree is created once and reused across
multiple expectation/verdict rounds for the same behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def ensure_worktree(project_root: Path, behaviour_id: str) -> Path:
    """Return the worktree path for *behaviour_id*, creating it if needed.

    Idempotent: reuses an existing worktree/branch across rounds rather than
    recreating it.
    """
    worktree_path = project_root / ".relay" / "work" / behaviour_id

    if (worktree_path / ".git").exists():
        return worktree_path

    branch = f"relay/{behaviour_id}"
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=project_root,
        capture_output=True,
        check=True,
    )
    return worktree_path


def current_branch(worktree_path: Path) -> str:
    """Return the checked-out branch name in *worktree_path*."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def has_worktree(project_root: Path, behaviour_id: str) -> bool:
    """True if a worktree already exists for *behaviour_id*."""
    return (project_root / ".relay" / "work" / behaviour_id / ".git").exists()
