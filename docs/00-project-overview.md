# Brigade — Project Overview

This document is background context for an AI coding assistant (Deepseek) that will implement this project phase by phase. Read this fully before starting any phase. Each phase will be handed to you as a separate document with its own scope and acceptance criteria — do not implement beyond the phase you're given.

## What this is

An implementation of **the Relay Method**, a multi-agent software-development workflow described here:
- https://a4al6a.substack.com/p/the-relay-method
- https://a4al6a.substack.com/p/expectation-driven-development-a (the validation practice the Examiner role is built on)

The Relay Method splits AI-assisted development across five specialized agents in a line, each allowed to talk only to its neighbours:

```
Owner (human) ⇄ Interpreter ⇄ Analyst ⇄ Examiner ⇄ Builder
```

- **Owner** — the human. States problems in plain language, approves plans, gates increments.
- **Interpreter** — the human-facing agent. Restates the Owner's problem as a *need*, never a solution. Talks to the Owner live; talks to the Analyst via files.
- **Analyst** — turns a need into an observable **behaviour** (what must be true, no mention of *how*).
- **Examiner** — decomposes a behaviour into precise, checkable **expectations** (`E1..En`), demands **evidence** they hold, and issues a **verdict**. This role implements **Expectation-Driven Development (EDD)** — see the second article above.
- **Builder** — the only role that touches code. Receives expectations, writes and runs real code, and reports back *only* which expectations are now satisfied plus evidence — never implementation detail.

A sixth role, the **Sentinel**, audits the whole conversation for contract violations. **It is out of scope for this build — phase 2 of the project, not part of any phase below.**

## The two changes from the original article

1. **The Interpreter is not a separate process.** It *is* the coding harness the human is already talking to — Claude Code or opencode. There is no standalone "Interpreter agent" to build; instead, the harness is configured (via `AGENTS.md`/`CLAUDE.md` and an MCP server) to behave as the Interpreter.
2. **Every other role's model is configurable.** Analyst, Examiner, and Builder each get their own provider/model config, so e.g. the Examiner might run on Claude Sonnet while the Builder runs on Deepseek via OpenRouter.

## Core architectural decisions (already made — do not revisit these)

- **Local machine only.** No remote hosts, no systemd, no multi-machine sync. All daemons run as async tasks inside one foreground process (`brigade up`).
- **One Interpreter session per project directory.** A "new project" is just a new directory with its own `.brigade/` state — never one long-running session juggling multiple projects.
- **Distribution: installable CLI**, not a template repo to clone. Installed via `uv tool install` (from git initially — no need to publish to PyPI).
- **Topology is a fixed constant in code, not a config file.** The five roles, their edges, and which message types are legal on each edge never vary between projects — only the *models* behind each role do. Full topology:

  | From | To | Message type(s) |
  |---|---|---|
  | Interpreter | Analyst | `behaviour-to-implement` |
  | Analyst | Examiner | `behaviour` |
  | Examiner | Builder | `expectation`, `verdict` |
  | Builder | Examiner | `evidence` |
  | Examiner | Analyst | `behaviour-status` |
  | Analyst | Interpreter | `behaviour-status` |

  `behaviour-status` is **re-authored at each hop**, not forwarded verbatim — the Examiner's version can reference expectations, the Analyst's stripped-down version cannot. These are two separate messages linked by `reply_to`.

  Owner↔Interpreter also has a fixed vocabulary (`problem`, `clarification`, `roadmap`, `roadmap-verdict`, `increment`, `continue-query`, `feedback`, `result`, `question`) but this edge is the live chat itself, not a mailbox — see Interpreter section below.

- **Inter-agent messages: full JSON envelope. Owner↔Interpreter: natural language only.** JSON is used for machine-to-machine edges because the recipient is code before it's ever read by a model. The Owner↔Interpreter edge is a live conversation — no envelope, no structured output required of the model there. Content *inside* JSON fields (a behaviour's description, an expectation's statement, evidence's claim) is still natural language — only the envelope (`id`, `type`, `from`, `to`, `behaviour_id`, `reply_to`, `created_at`) is structured.
- **Ledger + mailboxes, not a message broker.** Every message is written once, permanently, to `.brigade/ledger/<ulid>.json`. A mailbox `inbox/` is just a pointer file referencing an unconsumed ledger entry — deleted once the recipient processes it. The ledger is git-committed (permanent audit trail); mailboxes are gitignored (transient queue state).
- **Evidence is real tests with execution receipts, not narration.** Per EDD, the Builder's evidence must show what it actually ran (`command` + `raw_output`), not a description of expected behavior. Evidence carries a `confidence` tier (`executed` / `partial` / `narrative`) so weaker evidence (e.g. some UI states) is never silently trusted like a real test run.
- **The Builder runs as a headless coding-harness session** (`claude -p` / opencode non-interactive), not a custom agent loop — reusing existing agentic tooling rather than reinventing it. Fresh session per behaviour (not resumed across behaviours) for v1. Isolated in its own git worktree per behaviour (`.brigade/work/<behaviour_id>`, branch `brigade/<behaviour_id>`), merged on Examiner acceptance.
- **The `expectation → evidence → verdict` loop is capped**, config value `max_loops`, default `3`, **project-level only for v1** (no per-behaviour override yet). `loop_count`/`max_loops` live inside the message payload itself, not in worker memory, so a crashed/restarted worker doesn't lose the count. On cap-out, the Examiner escalates upward (`behaviour-status: blocked`) instead of looping forever.
- **Two distinct kinds of retry — do not conflate them:**
  1. *Schema/topology retry* — a malformed or wrong-neighbour message is rejected before being written, fed back to the model to correct. A robustness concern, not user-configurable.
  2. *EDD expectation loop* — the `max_loops`-capped cycle above. A real, configurable design knob.
- **Config is per-project (`.brigade/config.toml`), not per-role hardcoded.** Model strings use a `provider/model` format (LiteLLM-style). No API keys ever live in config — they come from the environment. `[roles.builder]` additionally needs a `harness` field (`claude` or `opencode`) since it's the one role that isn't a single LLM call.
- **Model capability table starts small: Anthropic, OpenAI, Deepseek only.** Other providers/models work via config but fall back to an "unknown model" path rather than being auto-known. A `capabilities.model_overrides` section in config is the escape hatch for filling in a missing/incorrect entry by hand.
- **Persona prompts live in `.brigade/personas/<role>.md`, optional.** If absent, a built-in default is used. Every persona prompt should cover: (1) identity at its abstraction level, (2) explicit allowed neighbours (restated even though the validator enforces it — reduces wasted round-trips with weaker models), (3) a concrete forbidden-leakage boundary with negative examples, (4) its output contract.

## Directory layout (per project)

```
myproject/                    # the codebase being built
├── .brigade/
│   ├── config.toml            # per-project model config
│   ├── mailboxes/
│   │   ├── analyst/inbox/
│   │   ├── examiner/inbox/
│   │   ├── builder/inbox/
│   │   └── interpreter/inbox/  # only receives behaviour-status
│   ├── ledger/                 # permanent, one file per message, git-committed
│   ├── personas/                # optional per-role prompt overrides
│   ├── work/                    # git worktrees, one per in-flight behaviour
│   └── state.json               # runtime bookkeeping (pids, current status) — not source of truth
├── .mcp.json                    # wires the harness to the Interpreter's brigade MCP tools
└── AGENTS.md / CLAUDE.md        # Interpreter persona + rules
```

`.brigade/mailboxes/` and `.brigade/state.json` are gitignored. `.brigade/ledger/`, `config.toml`, and `personas/` are committed.

## CLI shape

```
brigade init [dir]     # scaffold .brigade/ into a new or existing directory
brigade up               # foreground process: all role workers as async tasks, live status
brigade status            # what's in flight, inbox depths
brigade down
```

The Interpreter itself is **not** started via this CLI — it's just `claude` or `opencode` run normally inside the project directory, picking up `.mcp.json` and `AGENTS.md`/`CLAUDE.md` automatically.

## Tech stack expectations

Python, managed with `uv`. `pip install --break-system-packages` is not relevant here — this is a real project with `pyproject.toml` and `uv`-managed dependencies, not a throwaway script. A model-routing library (e.g. LiteLLM) is expected for the provider/model abstraction rather than hand-rolling per-provider clients.

## What "done" looks like for this whole project

A behaviour typed into a `claude`/`opencode` session in a scaffolded project directory flows all the way down to the Builder, produces real executed evidence, gets a verdict, and climbs back up to a increment the Owner can see and approve — with every message along the way recorded permanently in `.brigade/ledger/`.
