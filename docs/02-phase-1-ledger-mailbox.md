# Phase 1 — Ledger and Mailbox Primitives

Read `00-project-overview.md` first. This phase is the foundation everything else depends on, and — deliberately — involves no LLM calls at all. Everything here should be testable with hand-constructed fake messages.

## Goal

A message envelope type, a fixed topology table, a validator that enforces it, and a ledger/mailbox storage layer that's crash-safe by construction (all state is files on disk — nothing lives only in memory).

## Deliverables

### 1. Message envelope

A data structure (e.g. a `dataclass`/`pydantic` model) with these fields, matching the project overview exactly:

```
id             str   — ULID, sortable, becomes the ledger filename
type           str   — one of the fixed vocabulary (see topology table below)
from_role      str
to_role        str
behaviour_id   str   — groups every message belonging to one behaviour's round trip
reply_to       str | None
created_at     datetime
schema_version int
payload        dict  — type-specific body, see per-type shapes below
```

### 2. Per-type payload shapes

Implement validation (e.g. via `pydantic` models, one per message type) for:

- `behaviour-to-implement`: `{ text: str }`
- `behaviour`: `{ actor: str, outcome: str, boundaries: str }`
- `expectation`: `{ expectations: [{id: str, statement: str}], integration_expectation: str, loop_count: int, max_loops: int }`
- `evidence`: `{ evidence: [{expectation_id: str, claim: str, execution: {command: str, raw_output: str, artifact_ref: str | None}, confidence: "executed" | "partial" | "narrative"}], test_files_touched: [str] }`
- `verdict`: `{ satisfied: [str], unmet: [{expectation_id: str, reason: str}], loop_count: int, escalate: bool }`
- `behaviour-status`: `{ behaviour_id: str, outcome: "solved" | "blocked" | "partial", summary: str }`

The Owner↔Interpreter types (`problem`, `clarification`, `roadmap`, `roadmap-verdict`, `increment`, `continue-query`, `feedback`, `result`, `question`) can use a looser shared payload shape (e.g. just `{ text: str }`) since they're logged, not validated against a machine consumer — but still validate that `type` is one of these known values.

### 3. Fixed topology table

A constant (not read from any config file) encoding the table from the project overview:

```
(interpreter, analyst)  → {behaviour-to-implement}
(analyst, examiner)     → {behaviour}
(examiner, builder)     → {expectation, verdict}
(builder, examiner)     → {evidence}
(examiner, analyst)     → {behaviour-status}
(analyst, interpreter)  → {behaviour-status}
```

Plus a way to represent that Owner↔Interpreter message types are valid but not mailbox-routed (see mailbox section below — this edge never touches `.brigade/mailboxes/`).

### 4. Validator

A function `validate(message) -> Ok | ValidationError` that checks, in order:
1. `(from_role, to_role)` is a real edge in the topology table.
2. `type` is one of the types legal on that specific edge.
3. `payload` matches the schema for `type`.

Return a structured error (not just a raised exception with a string) so a calling worker can feed the specific problem back to a model for a corrective retry. Do not silently coerce or fix invalid messages — reject and report.

### 5. Ledger writer/reader

- `write_message(message) -> None`: validates, then writes `payload` + full envelope as one JSON file to `.brigade/ledger/<id>.json`. Writing must be atomic (write to a temp file, then rename) — never leave a partially-written ledger file if the process dies mid-write.
- `read_message(id) -> Message`
- `list_ledger(behaviour_id: str | None = None) -> list[Message]`: sorted by `id` (ULIDs sort chronologically), optionally filtered to one behaviour — this is what `brigade status`/a future TUI will use.
- The ledger is append-only in practice: nothing in this phase should ever modify or delete a file under `.brigade/ledger/`.

### 6. Mailbox (inbox) mechanics

- `deliver(message) -> None`: calls `write_message`, then creates a pointer in `.brigade/mailboxes/<to_role>/inbox/<id>` (an empty marker file, or a tiny file containing just the ledger path — either is fine, but don't duplicate the message content into it).
- `list_inbox(role) -> list[str]`: returns pending message ids for a role, oldest first.
- `consume(role, id) -> Message`: reads the full message from the ledger, deletes the inbox pointer, returns the message. This is what a worker calls when it picks up a message to act on.
- Owner↔Interpreter messages (see topology note above) are written to the ledger via `write_message` directly but **never** go through `deliver`/an inbox — there is no `interpreter` mailbox for messages *from* the Owner, since that edge is live chat, not file-polled. (The `interpreter` inbox that *does* exist is solely for incoming `behaviour-status` from the Analyst.)

## Out of scope for this phase

- Any actual LLM/model calls.
- The worker loop that watches an inbox and reacts (Phase 2).
- `config.toml` parsing (touched lightly if needed for paths, but role/model config itself is Phase 2+).
- Git worktrees, headless harness invocation (Phase 3).

## Acceptance criteria

- [ ] A valid `behaviour-to-implement` message from `interpreter` to `analyst` round-trips: construct it, `deliver()` it, `list_inbox("analyst")` shows it, `consume("analyst", id)` returns the identical message, and the pointer is gone from the inbox afterward while the ledger file remains.
- [ ] A message on a nonexistent edge (e.g. `builder` → `interpreter`) is rejected by the validator with a clear structured error, and nothing is written to disk.
- [ ] A message with a legal edge but wrong `type` for that edge (e.g. `evidence` sent `interpreter` → `analyst`) is rejected the same way.
- [ ] A message with a well-formed envelope but a payload that doesn't match its type's schema (e.g. an `expectation` missing `loop_count`) is rejected.
- [ ] Killing the process mid-write (simulate by interrupting or mocking) never leaves a corrupt/partial file under `.brigade/ledger/`.
- [ ] `list_ledger(behaviour_id=X)` correctly filters to only messages carrying that `behaviour_id`.
- [ ] `list_ledger()` with no filter returns all messages in chronological (ULID) order.
- [ ] Two `behaviour-status` messages for the same behaviour (one `examiner`→`analyst`, one `analyst`→`interpreter`) can be written as two distinct ledger entries linked via `reply_to`, and both are retrievable.
