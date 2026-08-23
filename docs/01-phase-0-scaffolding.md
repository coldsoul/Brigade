# Phase 0 — Scaffolding

Read `00-project-overview.md` first if you haven't already. This phase builds no agent logic at all — it proves the project structure and CLI entry points exist and are wired together correctly.

## Goal

A installable CLI called `brigade` that can scaffold a new project's `.brigade/` directory, and stub commands for the rest of the lifecycle, with no real behaviour behind them yet beyond producing correct files and sensible output.

## Deliverables

1. **Package skeleton**: `pyproject.toml` (managed with `uv`), console-script entry point named `brigade`, source layout of your choice (a `src/brigade/` package is a reasonable default).
2. **`brigade init [DIR]`**
   - Defaults to the current directory if `DIR` is omitted.
   - Creates the full directory layout described in the project overview:
     ```
     .brigade/
       config.toml
       mailboxes/{analyst,examiner,builder,interpreter}/inbox/
       ledger/
       personas/
       work/
       state.json
     .mcp.json
     AGENTS.md   (or CLAUDE.md — see note below)
     ```
   - `config.toml` is created with placeholder/sensible-default values (see `config.toml` shape in the project overview — you can stub reasonable defaults now, this gets filled in properly in a later phase).
   - `state.json` starts as an empty/minimal valid JSON object.
   - Refuses to overwrite an existing `.brigade/` directory unless passed an explicit `--force` flag.
   - Adds a `.gitignore` (or appends to an existing one) covering `.brigade/mailboxes/`, `.brigade/state.json`, and `.brigade/work/`.
3. **`brigade up`** — stub only for this phase. Prints a message indicating it would start the role workers, and exits (or blocks with a placeholder "watching for messages..." loop). Real worker logic is later phases.
4. **`brigade status`** — stub only. Reads `.brigade/` if present and prints basic info (e.g. "no `.brigade/` found in this directory" vs. "found, N pending messages" — the count can be `0`/hardcoded for now since mailboxes aren't populated yet).
5. **`brigade down`** — stub only, can be a no-op for now since `brigade up` isn't a real background process yet.
6. All four commands should fail with a clear error message (not a stack trace) when run outside a `brigade init`'d directory, except `init` itself.

## Note on `AGENTS.md` vs `CLAUDE.md`

Scaffold both if uncertain which harness will be used, or pick one and document the assumption clearly in the README — this isn't worth overthinking in this phase. The actual persona *content* written into this file is Phase 4's job; for now it can be a placeholder comment noting it will be filled in later.

## Out of scope for this phase

- Anything that reads or writes a mailbox message.
- Any LLM/model calls.
- The topology table, message envelope, or validator (Phase 1).
- Any real "watching" logic in `brigade up` (Phase 2+).

## Acceptance criteria

- [ ] `uv tool install` (or `uv run`) makes the `brigade` command available.
- [ ] `brigade init myproject` in an empty directory produces exactly the layout above, with no errors.
- [ ] Running `brigade init` again on the same directory fails with a clear message; `brigade init --force` overwrites cleanly.
- [ ] `brigade status` run outside any `.brigade/`-containing directory prints a clear "not a brigade project" message rather than crashing.
- [ ] `brigade status` run inside a freshly-initialized project runs without error.
- [ ] Basic `--help` output exists for the CLI and each subcommand.
