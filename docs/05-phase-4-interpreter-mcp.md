# Phase 4 — Interpreter MCP Server

Read `00-project-overview.md` and the prior phase documents first. This phase is what turns "a pipeline that runs" into "something the Owner actually talks to." No new worker daemon is created here — the Interpreter is the harness itself (Claude Code / opencode), not a background process.

## Goal

An MCP server, spawned as a subprocess of the harness session (stdio, no separate lifecycle to manage), exposing tools that let the Interpreter dispatch behaviours downward and check on their status — plus the persona content (`AGENTS.md`/`CLAUDE.md`) that makes the harness actually *behave* as the Interpreter role.

## Deliverables

### 1. MCP server with two core tools

- **`dispatch_behaviour(text: str) -> { behaviour_id: str }`**
  - Constructs a `behaviour-to-implement` message (envelope fields assembled by code, `text` is the only model-supplied content) `from: interpreter, to: analyst`.
  - Validates and delivers it via the Phase 1 primitives.
  - Returns immediately with the new `behaviour_id` — does **not** block waiting for a result. Blocking here would tie up the harness's turn for however long the whole pipeline takes.

- **`check_status(behaviour_id: str) -> { outcome: "pending" | "solved" | "blocked" | "partial", summary: str | None }`**
  - Reads `.relay/mailboxes/interpreter/inbox/` and `.relay/ledger/` for any `behaviour-status` message matching `behaviour_id`.
  - If found: consumes it (clears the inbox pointer, keeps the ledger entry), returns its outcome/summary.
  - If not found: returns `"pending"`.
  - This is designed to be called repeatedly by the harness's own agentic loop (poll pattern) rather than the tool call itself blocking/sleeping for a long time — see the "waiting" discussion in the project background: prefer the harness re-invoking this tool over several of its own turns rather than one tool call sleeping indefinitely.

### 2. Ledger-logging side effect for the live conversation

Since the Owner↔Interpreter edge is natural language, not files, the Interpreter's *persona* (not this MCP server acting alone) should be instructed to call a third tool at the right moments so the full chain stays replayable in one place:

- **`log_conversation(type: "problem" | "clarification" | "roadmap" | "roadmap-verdict" | "increment" | "continue-query" | "feedback" | "result" | "question", text: str) -> None`**
  - Writes a message of the given type directly to `.relay/ledger/` (via `write_message`, not `deliver` — this edge has no inbox, per Phase 1).
  - `from`/`to` are always `interpreter`/`owner` or `owner`/`interpreter` depending on type — infer sensibly from the type, or accept a `from_role` argument if that's cleaner.
  - This should feel like a side effect the model triggers as part of normal conversation, not something the Owner ever sees or has to think about.

### 3. Persona content (`AGENTS.md` / `CLAUDE.md`)

Write the actual Interpreter persona that gets scaffolded by `relay init` (Phase 0 stubbed this file — this phase fills it in for real). It should instruct the harness to:
- Speak only in terms of needs and outcomes with the Owner — never propose solutions, architecture, or implementation approaches directly.
- Ask clarifying questions *before* proposing a roadmap, so ambiguity is resolved rather than assumed (call `log_conversation` around these).
- Propose a roadmap of potentially-shippable increments and get a `roadmap-verdict` before dispatching the first behaviour.
- Dispatch one behaviour at a time via `dispatch_behaviour`, then poll `check_status` (its own subsequent turns, not one long blocking call) until it resolves.
- On `"solved"`, present the increment to the Owner and ask whether to continue (`continue-query`), never surfacing what the Analyst/Examiner/Builder actually did internally — only that the behaviour is complete.
- On `"blocked"` (loop cap hit), surface this honestly to the Owner as a `question`/blocker rather than pretending success — this is a real, expected outcome, not a bug to hide.

### 4. `.mcp.json` scaffolding

Update Phase 0's `relay init` output so the generated `.mcp.json` correctly points the harness at this MCP server (however your chosen harness expects a local/stdio MCP server to be declared).

## Out of scope for this phase

- Any change to Analyst/Examiner/Builder worker logic (Phases 2–3 are already complete and untouched by this phase).
- A TUI or any visual dashboard for `relay up` — plain log output remains sufficient.
- Sentinel — still deferred.

## Acceptance criteria

- [ ] Starting `claude` (or `opencode`) inside a `relay init`'d project directory picks up the MCP server automatically with no manual configuration step.
- [ ] Typing a plausible feature request to the Interpreter results in it asking at least one clarifying question before proposing anything resembling a plan (verify this is genuine behavior, not skipped).
- [ ] After a roadmap is agreed, dispatching the first behaviour calls `dispatch_behaviour` and does not block the conversation — the Interpreter should be able to say something like "working on it" and check back, not freeze mid-turn.
- [ ] With `relay up` running the Phase 2/3 workers in another terminal, a real behaviour dispatched through the Interpreter flows all the way down and back, and `check_status` eventually returns `"solved"` with a sensible summary.
- [ ] The Owner-facing summary presented for a solved behaviour contains no implementation detail (no function/file/library names) — confirms the leakage boundary holds all the way up the chain, not just at the Builder→Examiner edge.
- [ ] Forcing a `"blocked"` outcome (e.g. an expectation that can't realistically be satisfied) results in the Interpreter honestly telling the Owner it's blocked, rather than claiming success or silently retrying forever.
- [ ] `.relay/ledger/` after a full conversation contains a complete, replayable trail from the Owner's first message through to the final increment — spot-check that `reply_to` chains are intact end to end.
