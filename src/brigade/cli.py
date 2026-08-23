"""Brigade CLI — entry point for all brigade commands."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

CONFIG_TOML_TEMPLATE = """\
# Brigade configuration
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

[roles.designer]
model = "anthropic/claude-sonnet-5"
harness = "claude"
review_tool = "auto"    # "auto" | "lavish" | "basic"

[roles.sentinel]
model = "anthropic/claude-sonnet-5"
scan_every = 10         # scan after this many new ledger messages

[capabilities.model_overrides]
# escape hatch — empty by default, filled in only when the built-in table is wrong
"""

MCP_JSON_TEMPLATE = """\
{
  "mcpServers": {
    "brigade": {
      "command": "brigade",
      "args": ["mcp"]
    }
  }
}
"""

OPENCODE_JSON_TEMPLATE = """\
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "brigade": {
      "type": "local",
      "command": ["brigade", "mcp"],
      "enabled": true
    }
  }
}
"""

AGENTS_MD_TEMPLATE = """\
# Brigade Interpreter

You are the Interpreter in the Relay Method — the human-facing agent, talking
live with the Owner. You restate the Owner's problems as *needs*, never as
solutions.

## Classify every message first

Not every message enters the pipeline. Before responding to any Owner message,
decide which case it is:

- If the request only needs reading or explaining what already exists (how
  something works, how to run it, why it behaves a certain way), answer directly
  using your read tools. Never call `dispatch_behaviour` for this.
- If fulfilling the request means the codebase needs to change — even a one-line
  change — that must go through `dispatch_behaviour`. Never edit project code
  yourself, regardless of how small the change looks. The pipeline's guarantees
  only hold if every change passes through it.
- If the request asks for visual/creative direction rather than a concrete
  change, route to `dispatch_design_request` instead.
- If a question reveals something that should change ("why doesn't X work" →
  "it doesn't, and it should"), answer the question first, then ask the Owner
  whether they want it fixed — only dispatch once that's confirmed, not
  automatically.

When you answer directly (no dispatch), still log the exchange via
`log_conversation` — `question` for the query and `result` for your answer — so
the conversation stays replayable even without a pipeline run.

## Hard boundaries — you do not write code

You have five brigade tools: `dispatch_behaviour`, `check_status`,
`dispatch_design_request`, `check_design_status`, and `log_conversation`.

You may READ the project to answer questions — list files, read files, search,
and run read-only commands. You must NEVER write or edit a file yourself, not
even a one-line fix. Every codebase change goes through `dispatch_behaviour`;
the Builder writes all code — never you.

Do not inspect `.mcp.json`, `opencode.json`, `.brigade/`, or the brigade source code
— those are opaque plumbing, out of your lane.

If a brigade tool errors, report it to the Owner. Do not attempt to debug brigade.

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
- `dispatch_design_request(text)` — send a creative/visual exploration request
  to the Designer. Returns a `behaviour_id` immediately and does NOT block.
- `check_design_status(behaviour_id)` — poll for a design result (an approved
  HTML concept). Returns `artifact_ref` and `description` when done.
- `log_conversation(type, text)` — record an Owner↔Interpreter message in the
  permanent ledger so the whole conversation stays replayable.

## Design exploration (when the request is visual, not concrete)

When the Owner asks what something should look like — a layout, a page, a
screen, a visual direction — before there is a concrete behaviour to build,
route it through the Designer rather than the Builder:

1. Dispatch the design request via `dispatch_design_request`, then poll
   `check_design_status` until it returns `done`.
2. Present the returned `description` to the Owner for approval.
3. Once the Owner approves the direction, fold the returned `artifact_ref`
   (the path to the approved HTML file) into the text of the eventual
   `behaviour-to-implement` you send via `dispatch_behaviour`, so the Builder
   can see the agreed design while it works.
4. If the design result indicates the iteration cap was reached (the
   description says so), surface the choice to the Owner honestly: proceed
   with the current concept as-is, or abandon the design exploration.

## Workflow — for a code change only

Follow this only after classification lands on "the codebase needs to change":

1. Restate the need. If anything is ambiguous, ask a clarifying question FIRST
   and log it (`log_conversation` with `clarification`). Do not assume.
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
# Brigade — transient state (managed by `brigade init`)
.brigade/mailboxes/
.brigade/state.json
.brigade/work/
.brigade/evidence/
.brigade/logs/
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_brigade_dir() -> Path | None:
    """Walk up from cwd looking for a `.brigade/` directory.

    Returns the path to the `.brigade/` directory if found, or None.
    """
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        brigade_dir = parent / ".brigade"
        if brigade_dir.is_dir():
            return brigade_dir
    return None


def _require_brigade_project():
    """Exit with a clear message if not inside a brigade-initialized directory."""
    brigade_dir = _find_brigade_dir()
    if brigade_dir is None:
        click.echo(
            "error: not a brigade project — no `.brigade/` directory found here "
            "or in any parent directory. Run `brigade init` first.",
            err=True,
        )
        sys.exit(1)
    return brigade_dir


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
    """Append brigade-specific entries to `.gitignore` in *target_dir*.

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
    """Brigade — multi-agent software-development workflow."""


# ---------------------------------------------------------------------------
# brigade init
# ---------------------------------------------------------------------------

@main.command()
@click.argument("directory", required=False, default=".")
@click.option("--force", is_flag=True, help="Overwrite an existing .brigade/ directory.")
def init(directory: str, force: bool):
    """Scaffold a new Brigade project in DIRECTORY (defaults to current directory)."""
    target = Path(directory).resolve()
    brigade_dir = target / ".brigade"

    # --- guard: refuse to overwrite unless --force -------------------------
    if brigade_dir.exists():
        if not force:
            click.echo(
                f"error: {brigade_dir} already exists. Use --force to overwrite.",
                err=True,
            )
            sys.exit(1)
        _rmtree_safe(brigade_dir)
        click.echo(f"Removed existing {brigade_dir}")

    # --- create directory layout -------------------------------------------
    target.mkdir(parents=True, exist_ok=True)

    directories = [
        brigade_dir,
        brigade_dir / "mailboxes" / "analyst" / "inbox",
        brigade_dir / "mailboxes" / "examiner" / "inbox",
        brigade_dir / "mailboxes" / "builder" / "inbox",
        brigade_dir / "mailboxes" / "interpreter" / "inbox",
        brigade_dir / "mailboxes" / "designer" / "inbox",
        brigade_dir / "ledger",
        brigade_dir / "personas",
        brigade_dir / "work",
    ]
    for d in directories:
        d.mkdir(parents=True, exist_ok=True)

    # --- write files -------------------------------------------------------
    (brigade_dir / "config.toml").write_text(CONFIG_TOML_TEMPLATE)
    (brigade_dir / "state.json").write_text("{}\n")
    (target / ".mcp.json").write_text(MCP_JSON_TEMPLATE)
    (target / "opencode.json").write_text(OPENCODE_JSON_TEMPLATE)
    (target / "AGENTS.md").write_text(AGENTS_MD_TEMPLATE)

    # --- .gitignore --------------------------------------------------------
    _append_gitignore(target)

    # --- git init (user requirement) ---------------------------------------
    new_repo = _init_git_repo(target)

    click.echo(f"Initialized Brigade project in {target}")
    if new_repo:
        click.echo("  (also initialized a new git repository)")


def _rmtree_safe(path: Path):
    """Remove a directory tree, raising on failure."""
    import shutil

    shutil.rmtree(path)


# ---------------------------------------------------------------------------
# brigade up
# ---------------------------------------------------------------------------

@main.command()
@click.option("--quiet", is_flag=True, help="Only show warnings and errors.")
@click.option("--verbose", is_flag=True, help="Show debug output.")
def up(quiet: bool, verbose: bool):
    """Start the Brigade role workers as foreground tasks."""
    import threading
    import time

    from brigade.config import load_config
    from brigade.llm import LiteLLMRouter
    from brigade.logging_config import configure_logging
    from brigade.sentinel import Sentinel
    from brigade.workers import (
        AnalystWorker,
        BuilderWorker,
        DesignerWorker,
        ExaminerWorker,
    )

    brigade_dir = _require_brigade_project()
    config = load_config(brigade_dir)
    router = LiteLLMRouter()

    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    configure_logging(level=level, log_dir=brigade_dir / "logs")

    workers = [
        AnalystWorker(config, router, brigade_dir),
        ExaminerWorker(config, router, brigade_dir),
        BuilderWorker(config, router, brigade_dir),
        DesignerWorker(config, router, brigade_dir),
    ]

    sentinel = Sentinel(config, router, brigade_dir)

    threads = [
        threading.Thread(target=w.run, daemon=True, name=w.role)
        for w in workers
    ]
    threads.append(threading.Thread(target=sentinel.run, daemon=True, name="sentinel"))
    for t in threads:
        t.start()

    click.echo(
        f"Brigade workers started: {', '.join(w.role for w in workers)}, sentinel"
    )
    click.echo("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopping.")


# ---------------------------------------------------------------------------
# brigade status
# ---------------------------------------------------------------------------

@main.command()
def status():
    """Show current brigade project status."""
    from brigade.storage import list_inbox

    brigade_dir = _find_brigade_dir()
    if brigade_dir is None:
        click.echo("Not a brigade project — no `.brigade/` directory found.")
        click.echo("Run `brigade init` to create one.")
        return

    project_dir = brigade_dir.parent
    click.echo(f"Brigade project: {project_dir}")

    roles = ["analyst", "examiner", "builder", "interpreter", "designer"]
    depths = {role: len(list_inbox(role, brigade_dir)) for role in roles}
    total = sum(depths.values())
    click.echo(f"Pending messages: {total}")
    for role in roles:
        click.echo(f"  {role}: {depths[role]}")

    from brigade.sentinel import sentinel_summary

    flags = sentinel_summary(brigade_dir)
    if flags:
        click.echo("Sentinel flags:")
        for (severity, category), count in sorted(flags.items()):
            click.echo(f"  {severity}/{category}: {count}")
    else:
        click.echo("Sentinel flags: none")


# ---------------------------------------------------------------------------
# brigade down
# ---------------------------------------------------------------------------

@main.command()
def down():
    """Stop the Brigade workers (stub — real logic in later phases)."""
    _require_brigade_project()
    click.echo("No brigade workers running (stub).")


# ---------------------------------------------------------------------------
# brigade mcp (hidden — spawned by the harness via .mcp.json)
# ---------------------------------------------------------------------------

@main.command(hidden=True)
def mcp():
    """Run the Interpreter MCP server over stdio."""
    from brigade.mcp_server import run

    run()


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
