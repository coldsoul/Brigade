"""Relay CLI — entry point for all relay commands."""

import json
import os
import subprocess
import sys
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

CONFIG_TOML_TEMPLATE = """\
# Relay configuration
# Model strings use provider/model format (e.g. "anthropic/claude-sonnet-4-20250514")

max_loops = 3

[roles.interpreter]
# No model config needed — the Interpreter is the coding harness itself

[roles.analyst]
model = "anthropic/claude-sonnet-4-20250514"

[roles.examiner]
model = "anthropic/claude-sonnet-4-20250514"

[roles.builder]
model = "anthropic/claude-sonnet-4-20250514"
harness = "claude"
"""

MCP_JSON_TEMPLATE = """\
{
  "mcpServers": {}
}
"""

AGENTS_MD_TEMPLATE = """\
# Relay Interpreter

> This file configures the coding harness to act as the Relay Method's Interpreter role.
> The full persona will be filled in during Phase 4.
"""

GITIGNORE_ENTRIES = """\
# Relay — transient state (managed by `relay init`)
.relay/mailboxes/
.relay/state.json
.relay/work/
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_relay_dir() -> Path | None:
    """Walk up from cwd looking for a `.relay/` directory.

    Returns the path to the `.relay/` directory if found, or None.
    """
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        relay_dir = parent / ".relay"
        if relay_dir.is_dir():
            return relay_dir
    return None


def _require_relay_project():
    """Exit with a clear message if not inside a relay-initialized directory."""
    relay_dir = _find_relay_dir()
    if relay_dir is None:
        click.echo(
            "error: not a relay project — no `.relay/` directory found here "
            "or in any parent directory. Run `relay init` first.",
            err=True,
        )
        sys.exit(1)
    return relay_dir


def _init_git_repo(target_dir: Path) -> bool:
    """Initialize a git repository in *target_dir* if one doesn't already exist.

    Returns True if a repo was newly created, False if one already existed.
    """
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=target_dir,
            capture_output=True,
            check=True,
        )
        return False  # already a git repo
    except subprocess.CalledProcessError:
        pass

    subprocess.run(
        ["git", "init"],
        cwd=target_dir,
        capture_output=True,
        check=True,
    )
    return True


def _append_gitignore(target_dir: Path) -> bool:
    """Append relay-specific entries to `.gitignore` in *target_dir*.

    Skips entries that already exist in the file.  Returns True if any new
    entries were appended, False otherwise.
    """
    gitignore = target_dir / ".gitignore"
    existing = set()
    if gitignore.exists():
        existing = set(gitignore.read_text().splitlines())

    new_lines = [
        line
        for line in GITIGNORE_ENTRIES.splitlines()
        if line not in existing and line.strip()
    ]

    if not new_lines:
        return False

    with gitignore.open("a") as f:
        # ensure a leading newline if the file already had content
        if existing:
            f.write("\n")
        f.write("\n".join(new_lines) + "\n")
    return True


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option()
def main():
    """Relay — multi-agent software-development workflow."""


# ---------------------------------------------------------------------------
# relay init
# ---------------------------------------------------------------------------

@main.command()
@click.argument("directory", required=False, default=".")
@click.option("--force", is_flag=True, help="Overwrite an existing .relay/ directory.")
def init(directory: str, force: bool):
    """Scaffold a new Relay project in DIRECTORY (defaults to current directory)."""
    target = Path(directory).resolve()
    relay_dir = target / ".relay"

    # --- guard: refuse to overwrite unless --force -------------------------
    if relay_dir.exists():
        if not force:
            click.echo(
                f"error: {relay_dir} already exists. Use --force to overwrite.",
                err=True,
            )
            sys.exit(1)
        _rmtree_safe(relay_dir)
        click.echo(f"Removed existing {relay_dir}")

    # --- create directory layout -------------------------------------------
    target.mkdir(parents=True, exist_ok=True)

    directories = [
        relay_dir,
        relay_dir / "mailboxes" / "analyst" / "inbox",
        relay_dir / "mailboxes" / "examiner" / "inbox",
        relay_dir / "mailboxes" / "builder" / "inbox",
        relay_dir / "mailboxes" / "interpreter" / "inbox",
        relay_dir / "ledger",
        relay_dir / "personas",
        relay_dir / "work",
    ]
    for d in directories:
        d.mkdir(parents=True, exist_ok=True)

    # --- write files -------------------------------------------------------
    (relay_dir / "config.toml").write_text(CONFIG_TOML_TEMPLATE)
    (relay_dir / "state.json").write_text("{}\n")
    (target / ".mcp.json").write_text(MCP_JSON_TEMPLATE)
    (target / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE)

    # --- .gitignore --------------------------------------------------------
    _append_gitignore(target)

    # --- git init (user requirement) ---------------------------------------
    new_repo = _init_git_repo(target)

    click.echo(f"Initialized Relay project in {target}")
    if new_repo:
        click.echo("  (also initialized a new git repository)")


def _rmtree_safe(path: Path):
    """Remove a directory tree, raising on failure."""
    import shutil

    shutil.rmtree(path)


# ---------------------------------------------------------------------------
# relay up
# ---------------------------------------------------------------------------

@main.command()
def up():
    """Start the Relay role workers (stub — real logic in later phases)."""
    relay_dir = _require_relay_project()
    click.echo(f"Relay workers would start here ({relay_dir.parent})")
    click.echo("Waiting for messages... (press Ctrl+C to stop)")
    try:
        while True:
            click.pause()
    except KeyboardInterrupt:
        click.echo()


# ---------------------------------------------------------------------------
# relay status
# ---------------------------------------------------------------------------

@main.command()
def status():
    """Show current relay project status."""
    relay_dir = _find_relay_dir()
    if relay_dir is None:
        click.echo("Not a relay project — no `.relay/` directory found.")
        click.echo("Run `relay init` to create one.")
        return

    project_dir = relay_dir.parent
    click.echo(f"Relay project: {project_dir}")

    # mailboxes not populated yet, always report 0 for now
    click.echo("Pending messages: 0")


# ---------------------------------------------------------------------------
# relay down
# ---------------------------------------------------------------------------

@main.command()
def down():
    """Stop the Relay workers (stub — real logic in later phases)."""
    _require_relay_project()
    click.echo("No relay workers running (stub).")


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
