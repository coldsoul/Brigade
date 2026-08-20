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
# Model strings use provider/model format (e.g. "anthropic/claude-sonnet-5").
# API keys are never stored here — they come from the environment.

[project]
max_loops = 3

[roles.interpreter]
# No model config needed — the Interpreter is the coding harness itself

[roles.analyst]
model = "anthropic/claude-sonnet-5"

[roles.examiner]
model = "anthropic/claude-sonnet-5"

[roles.builder]
model = "anthropic/claude-sonnet-5"
harness = "claude"

[capabilities.model_overrides]
# escape hatch — empty by default, filled in only when the built-in table is wrong
"""

MCP_JSON_TEMPLATE = """\
{
  "mcpServers": {
    "relay": {
      "command": "relay",
      "args": ["mcp"]
    }
  }
}
"""

AGENTS_MD_TEMPLATE = """\
# Relay Interpreter

You are the Interpreter in the Relay Method — the human-facing agent, talking
live with the Owner. You restate the Owner's problems as *needs*, never as
solutions.

## What you may and may not do

- Speak to the Owner only in terms of needs and observable outcomes.
- NEVER propose solutions, architecture, technologies, or implementation
  approaches directly. That work happens deeper in the chain and must never
  leak up to the Owner.

## Your tools

- `dispatch_behaviour(text)` — send one behaviour downward to be implemented.
  Returns a `behaviour_id` immediately and does NOT block.
- `check_status(behaviour_id)` — poll for the result of a dispatched behaviour.
  Call this over your own subsequent turns, not in one long blocking call.
- `log_conversation(type, text)` — record an Owner↔Interpreter message in the
  permanent ledger so the whole conversation stays replayable.

## Workflow

1. When the Owner states a problem, restate it as a need. If anything is
   ambiguous, ask a clarifying question FIRST and log it (`log_conversation`
   with `clarification`). Do not assume.
2. Propose a roadmap of small, independently-shippable increments, log it
   (`roadmap`), and wait for the Owner's verdict (`roadmap-verdict`). Do not
   dispatch anything until the Owner approves.
3. Dispatch ONE behaviour at a time via `dispatch_behaviour`, then poll
   `check_status` on later turns until it resolves.
4. On `solved`: present the increment to the Owner in plain terms — no
   implementation detail — and ask whether to continue (`continue-query`).
5. On `blocked`: tell the Owner honestly that this behaviour is blocked (the
   expectation loop hit its cap). Surface it as a question or blocker — never
   pretend success, and never silently retry forever.
6. Log each of your Owner-facing turns (`log_conversation`) so the chain stays
   replayable end to end.

## Leakage boundary

Never surface what the Analyst, Examiner, or Builder did internally. The Owner
sees only needs, outcomes, and progress — never function names, file names,
library names, data structures, or code structure.
"""

GITIGNORE_ENTRIES = """\
# Relay — transient state (managed by `relay init`)
.relay/mailboxes/
.relay/state.json
.relay/work/
.relay/evidence/
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
    """Start the Relay role workers as foreground tasks."""
    import threading
    import time

    from relay.config import load_config
    from relay.llm import LiteLLMRouter
    from relay.workers import AnalystWorker, BuilderWorker, ExaminerWorker

    relay_dir = _require_relay_project()
    config = load_config(relay_dir)
    router = LiteLLMRouter()

    workers = [
        AnalystWorker(config, router, relay_dir),
        ExaminerWorker(config, router, relay_dir),
        BuilderWorker(config, router, relay_dir),
    ]

    threads = [
        threading.Thread(target=w.run, daemon=True, name=w.role)
        for w in workers
    ]
    for t in threads:
        t.start()

    click.echo(f"Relay workers started: {', '.join(w.role for w in workers)}")
    click.echo("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopping.")


# ---------------------------------------------------------------------------
# relay status
# ---------------------------------------------------------------------------

@main.command()
def status():
    """Show current relay project status."""
    from relay.storage import list_inbox

    relay_dir = _find_relay_dir()
    if relay_dir is None:
        click.echo("Not a relay project — no `.relay/` directory found.")
        click.echo("Run `relay init` to create one.")
        return

    project_dir = relay_dir.parent
    click.echo(f"Relay project: {project_dir}")

    roles = ["analyst", "examiner", "builder", "interpreter"]
    depths = {role: len(list_inbox(role, relay_dir)) for role in roles}
    total = sum(depths.values())
    click.echo(f"Pending messages: {total}")
    for role in roles:
        click.echo(f"  {role}: {depths[role]}")


# ---------------------------------------------------------------------------
# relay down
# ---------------------------------------------------------------------------

@main.command()
def down():
    """Stop the Relay workers (stub — real logic in later phases)."""
    _require_relay_project()
    click.echo("No relay workers running (stub).")


# ---------------------------------------------------------------------------
# relay mcp (hidden — spawned by the harness via .mcp.json)
# ---------------------------------------------------------------------------

@main.command(hidden=True)
def mcp():
    """Run the Interpreter MCP server over stdio."""
    from relay.mcp_server import run

    run()


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
