# Phase 3 — Builder

Read `00-project-overview.md`, `02-phase-1-ledger-mailbox.md`, and `03-phase-2-workers-analyst-examiner.md` first. The Builder reuses the generic worker loop from Phase 2 but is structurally different from Analyst/Examiner: it isn't a single LLM call, it's a spawned headless coding-agent session. This is deliberately the last worker built.

## Goal

A Builder worker that, on receiving an `expectation` message, spins up an isolated, headless coding-harness session to implement and prove the expectations, and reports back an `evidence` message containing real execution receipts — never a narrated description of what it believes would happen.

## Deliverables

### 1. Git worktree isolation per behaviour

On receiving an `expectation`:
- Create `.brigade/work/<behaviour_id>/` as a git worktree on a new branch `brigade/<behaviour_id>` (`git worktree add`), if one doesn't already exist for this behaviour (a behaviour may cycle through multiple `expectation`/`verdict` rounds — reuse the same worktree/branch across those rounds, don't recreate it each time).
- All Builder work for this behaviour happens inside that worktree — never in the main project tree.
- On the Examiner ultimately accepting the behaviour, the worktree's branch should be mergeable as a clean diff (this phase can implement the merge step, or leave it as a documented manual/CLI step if that's simpler for v1 — your call, but state which one you built).
- On `escalate: true` (loop cap hit) or explicit rejection, the worktree/branch should be left intact rather than deleted, so a human can inspect what the Builder attempted.

### 2. Headless harness invocation

- Read `harness` and `model` from `[roles.builder]` in `config.toml`.
- Invoke the configured harness (`claude -p ...` or opencode's non-interactive equivalent) as a subprocess, with:
  - working directory set to the behaviour's worktree,
  - the model override flag set from config,
  - a prompt built from the Builder's persona (forbidden-leakage rules — no function names, file names, libraries, or data structures allowed to leak into what climbs back up the chain) plus the `expectation` payload's `expectations` and `integration_expectation`.
- Capture the subprocess's full stdout/stderr for logging/debugging, even though only a distilled `evidence` message goes into the ledger.
- Fresh session per behaviour for v1 — do not implement session resumption across behaviours in this phase.

### 3. Evidence production — the critical part

The Builder's *last instructed action* inside its headless session should be to write the evidence report itself, using its own file-write tool, directly into the shape the `evidence` message payload needs — this keeps the "narrowness" enforced by what you told it to write, not by a second distillation pass afterward. Concretely, instruct it to produce, per expectation:

```json
{
  "expectation_id": "E1",
  "claim": "<plain-language statement of what now holds>",
  "execution": {
    "command": "<the literal command/tool call it ran>",
    "raw_output": "<the actual captured output, not a summary>",
    "artifact_ref": "<path to a screenshot/log file, if applicable, else null>"
  },
  "confidence": "executed" | "partial" | "narrative"
}
```

Rules to enforce, both in the persona prompt and (where mechanically possible) in validation:
- `confidence: "executed"` requires a non-empty `command` and `raw_output` that plausibly correspond to something that actually ran — this phase can do basic sanity checks (e.g. `raw_output` isn't empty, `command` isn't empty) even if it can't fully verify truthfulness.
- If an expectation genuinely cannot be executed in the loop (per the EDD article's third category — UI rendering without a real browser tool, production-only behavior, rate-limited third-party calls), the Builder must mark it `"partial"` or `"narrative"` rather than falsely claiming `"executed"`.
- Where the evidence is a test (the expected default lane per the project's earlier direction — see project overview), the payload's `test_files_touched` should list the actual test file(s) added/modified, so the Examiner can inspect the assertion's source, not just trust the claimed result.
- The Builder worker (the wrapper code, not the harness session) should read back whatever the harness session wrote and package it into a validated `evidence` message — do not skip Phase 1's validator just because the content came from a more complex source this time.

### 4. Loop handling

- The Builder also needs to react to `verdict` messages (not just `expectation`) in its inbox — a `verdict` with unmet expectations means resuming work in the *same* worktree/branch for another round, producing a fresh `evidence` message. Reuse the harness invocation logic from step 2; the harness is re-invoked in the same worktree with the unmet expectations + reasons from the verdict as additional context.
- Respect `loop_count`/`max_loops` as already tracked in the message payload (Phase 2 already handles the Examiner side of the cap — this phase just needs to make sure the Builder doesn't need its own separate counter; it trusts what's in the incoming message).

## Out of scope for this phase

- Session resumption across behaviours.
- The Interpreter/MCP layer (Phase 4).
- Automatic conflict resolution if a worktree merge fails — surfacing the conflict clearly is enough for v1.
- Vision-capable evidence review — that's the Examiner's concern (already covered in its persona from Phase 2), not something the Builder needs to implement.

## Acceptance criteria

- [ ] A hand-placed `expectation` message in the Builder's inbox results in a new git worktree being created at `.brigade/work/<behaviour_id>/` on a new branch, and the configured harness being invoked inside it.
- [ ] A trivially satisfiable expectation (e.g. "a function `add(a, b)` returns the sum of its arguments") results in a valid `evidence` message with `confidence: "executed"`, a real `command`, and real `raw_output` — verify by hand that the output actually corresponds to something that ran, not text the model invented.
- [ ] The evidence message contains no leaked implementation detail in the `claim` fields (spot-check against the forbidden-leakage rule) even though the underlying code obviously has function names, file names, etc.
- [ ] `test_files_touched` correctly lists real files that exist in the worktree after the run.
- [ ] A `verdict` message placed in the Builder's inbox with an unmet expectation causes the Builder to resume in the *same* worktree/branch (verify via git log or branch state — not a fresh worktree) and produce a new `evidence` message addressing the specific unmet expectation and reason from the verdict.
- [ ] An expectation that's explicitly not executable in the loop (e.g. something requiring a live third-party API you deliberately don't provide credentials for in the test) results in evidence marked `"partial"` or `"narrative"`, never falsely `"executed"`.
- [ ] Killing the Builder process mid-run and restarting `brigade up` does not lose the worktree or corrupt the branch — the behaviour's work-in-progress survives a crash.
