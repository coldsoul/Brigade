# Phase 2 — Generic Worker Loop, Analyst, Examiner

Read `00-project-overview.md` and `02-phase-1-ledger-mailbox.md` first — this phase builds directly on the envelope/ledger/mailbox primitives from Phase 1. This is the first phase that makes real LLM calls.

## Goal

One reusable worker loop, proven out on the two simplest roles — Analyst and Examiner — both of which are single structured LLM calls with no code execution and no spawned sub-sessions. Builder (Phase 3) reuses this same loop but adds a lot on top of it, so getting this loop right here matters.

## Deliverables

### 1. `config.toml` parsing (real, not stubbed)

Implement the shape from the project overview:

```toml
[project]
max_loops = 3

[roles.analyst]
model = "anthropic/claude-sonnet-5"

[roles.examiner]
model = "openrouter/anthropic/claude-sonnet-5"

[roles.builder]
harness = "opencode"
model = "openrouter/deepseek/deepseek-coder"

[capabilities.model_overrides]
# escape hatch — empty by default, filled in only when the built-in table is wrong/missing an entry
```

- `model` strings are `provider/model`, resolved via your model-routing library (e.g. LiteLLM) — do not hand-roll separate per-provider HTTP clients.
- No API keys in this file, ever — they come from the environment, standard per-provider env var names.
- `[roles.builder]` is the only role with a `harness` field.

### 2. Built-in model capability table

A small, hardcoded table (not user-facing config) covering **Anthropic, OpenAI, and Deepseek models only** for this phase — this is an explicit scope decision, not an oversight; other providers work through config but fall back to a conservative "assume nothing, use the safe/slow path" default rather than being auto-known. Capability worth tracking at minimum: whether the model supports structured/schema-constrained output (`strict` / `loose` / `none`).

### 3. Persona loading

`load_persona(role) -> str`: reads `.relay/personas/<role>.md` if present, else falls back to a built-in default persona string for that role, embedded in the package. Every default persona (see structure below) should include:
1. Identity at its abstraction level.
2. Its explicit allowed neighbours (restated in the prompt even though the validator enforces it).
3. A concrete forbidden-leakage boundary, with at least one negative example.
4. Its output contract (which message type/payload shape it must produce).

### 4. Generic worker loop

A function/class, roughly: `run_worker(role: str)`:
1. Poll `.relay/mailboxes/<role>/inbox/` (a simple sleep-poll loop is fine for this phase — no need for OS-level file-watching yet).
2. On a new message: `consume()` it.
3. Build a prompt from `load_persona(role)` + the incoming message's payload.
4. Call the model via the router, using structured/schema-constrained output when the capability table says the model supports it; otherwise generate freeform and parse, with a bounded retry (feed the specific validation error back to the model) on parse/schema failure. This is the *schema/topology retry* described in the project overview — keep it separate in code from the *EDD loop* concept below.
5. Construct the reply message: the **envelope fields are assembled by code** (`id`, `behaviour_id`, `reply_to`, `created_at`, `from_role`, `to_role`), not trusted to the model — only the payload's natural-language/content fields come from the model's output.
6. Validate (Phase 1's validator) and `deliver()` the reply.
7. Log clearly (stdout is fine for this phase — a real TUI is a later concern) what was consumed and what was produced.

This loop should be role-agnostic — Analyst and Examiner differ only in their persona and payload-construction logic, not in the loop mechanics.

### 5. Analyst role

- Consumes `behaviour-to-implement` from its inbox.
- Produces a `behaviour` message to `examiner`: `{ actor, outcome, boundaries }`, with every trace of *how* stripped out. Use the falling-piece example from the source article as a calibration reference for what "no implementation detail" looks like in the persona prompt.
- Also handles the **reverse edge**: when it receives `behaviour-status` in its own inbox (from Examiner), it does not forward it verbatim — it *re-authors* a new, further-stripped `behaviour-status` message to `interpreter`, linked via `reply_to`. This is a second, distinct code path in the Analyst worker, not just "relay the same payload."

### 6. Examiner role

- Consumes `behaviour` from its inbox.
- Produces an `expectation` message to `builder`: a set of `E1..En` plus one integration expectation, `loop_count` initialized appropriately, `max_loops` pulled from `config.toml`'s `[project]` section.
- Also consumes `evidence` from `builder` (this comes in Phase 3, but build the code path now): evaluates it using an adversarial checklist in its persona — do the numbers add up, did it dodge an edge case, what input would break this, does `confidence: narrative` evidence get held to a stricter bar than `executed`. Produces either:
  - a `verdict` back to `builder` (if expectations are unmet and `loop_count < max_loops`), incrementing `loop_count`, or
  - a `behaviour-status` to `analyst` (if all expectations are satisfied, **or** `loop_count` has hit `max_loops` — in the latter case, `outcome: "blocked"`, not a false "solved").

Since Builder doesn't exist yet in this phase, test the Examiner's evidence-handling path with hand-constructed fake `evidence` messages placed directly in its inbox.

## Out of scope for this phase

- Real code execution or a spawned coding-harness session (Phase 3 — Builder).
- The Interpreter MCP server / live chat integration (Phase 4).
- Git worktrees.
- Any TUI beyond simple log output.

## Acceptance criteria

- [ ] `config.toml` with the shape above parses correctly; a missing `[roles.builder].harness` is a clear config error, not a silent default.
- [ ] A hand-placed `behaviour-to-implement` message in the Analyst's inbox results in a valid `behaviour` message delivered to the Examiner's inbox, with no implementation detail present in `boundaries`/`outcome` (spot-check against the persona's forbidden-leakage rule).
- [ ] A hand-placed `behaviour` message in the Examiner's inbox results in a valid `expectation` message delivered to the Builder's inbox, with `loop_count` and `max_loops` populated correctly from config.
- [ ] A hand-placed `evidence` message with `confidence: "executed"` and all expectations satisfied results in a `behaviour-status(outcome: "solved")` to the Analyst.
- [ ] A hand-placed `evidence` message with an unmet expectation and `loop_count < max_loops` results in a `verdict` back to the Builder with `escalate: false` and an incremented `loop_count`.
- [ ] The same scenario with `loop_count == max_loops` results in `behaviour-status(outcome: "blocked")` instead of another `verdict` — the cap is actually enforced, not just tracked.
- [ ] A `behaviour-status` placed in the Analyst's inbox (simulating it coming from the Examiner) results in a **new**, distinct `behaviour-status` message to the Interpreter, linked via `reply_to` — verify it's not the same payload forwarded unchanged.
- [ ] Feeding the worker a deliberately malformed model response (mock the model call to return broken JSON) triggers the schema-retry path and eventually either succeeds or fails loudly — it never silently delivers an invalid message.
