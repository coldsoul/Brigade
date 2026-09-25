"""Git worktree isolation per behaviour.

Each behaviour gets its own worktree at `.brigade/work/<behaviour_id>/` on a
branch `brigade/<behaviour_id>`.  The worktree is created once and reused across
multiple expectation/verdict rounds for the same behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class CommitError(Exception):
    """Raised when a worktree commit cannot be produced."""


def _base_path(project_root: Path, behaviour_id: str) -> Path:
    return project_root / ".brigade" / "work" / f"{behaviour_id}.base"


def ensure_worktree(project_root: Path, behaviour_id: str) -> Path:
    """Return the worktree path for *behaviour_id*, creating it if needed.

    Idempotent: reuses an existing worktree/branch across rounds rather than
    recreating it.  Records the branch-point commit so a later commit step can
    tell a real commit apart from "the harness did nothing".
    """
    worktree_path = project_root / ".brigade" / "work" / behaviour_id

    if (worktree_path / ".git").exists():
        return worktree_path

    branch = f"brigade/{behaviour_id}"
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=project_root,
        capture_output=True,
        check=True,
    )
    _base_path(project_root, behaviour_id).write_text(base + "\n")
    return worktree_path


def worktree_base(project_root: Path, behaviour_id: str) -> str:
    """Return the commit the worktree branch was created from.

    Falls back to `git merge-base` for worktrees created before the base was
    recorded.
    """
    path = _base_path(project_root, behaviour_id)
    if path.is_file():
        return path.read_text().strip()

    branch = f"brigade/{behaviour_id}"
    result = subprocess.run(
        ["git", "merge-base", branch, "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_worktree(worktree_path: Path, base_commit: str, message: str) -> str:
    """Commit all changes in *worktree_path* and return the resulting commit hash.

    Raises `CommitError` when no commit was produced — either the harness made
    no code changes (branch still at *base_commit*), or `git` itself failed.
    """
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise CommitError(f"git add failed: {add.stderr.strip()}")

    commit = subprocess.run(
        [
            "git",
            "-c", "user.name=Brigade Builder",
            "-c", "user.email=brigade@localhost",
            "commit",
            "-m",
            message,
        ],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if commit.returncode == 0:
        return _head_commit(worktree_path)

    if "nothing to commit" in (commit.stderr + commit.stdout):
        head = _head_commit(worktree_path)
        if head and head != base_commit:
            return head  # the harness already committed its work
        raise CommitError("no changes to commit — the harness produced no code changes")

    raise CommitError(f"git commit failed: {(commit.stderr or commit.stdout).strip()}")


def _head_commit(worktree_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommitError(f"git rev-parse HEAD failed: {result.stderr.strip()}")
    return result.stdout.strip()


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
    return (project_root / ".brigade" / "work" / behaviour_id / ".git").exists()


def git_exclude(worktree_path: Path, pattern: str) -> None:
    """Append *pattern* to the worktree's local git exclude file.

    Keeps the pattern out of `git status`/commits without touching the committed
    `.gitignore` — the exclude file is per-clone (per-worktree) and never merged.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        check=True,
    )
    exclude_path = Path(result.stdout.strip())
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text().splitlines() if exclude_path.exists() else []
    if pattern not in existing:
        with exclude_path.open("a") as f:
            f.write(f"{pattern}\n")
