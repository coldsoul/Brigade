# Phase 6 — Designer

Read `00-project-overview.md` and the prior phase documents first, especially `04-phase-3-builder.md` — the Designer reuses the same "headless coding-harness session in its own worktree" pattern the Builder uses, but produces exploratory visual concepts instead of verified functional code, and is gated by a human choice rather than a pass/fail verdict.

## Goal

A Designer worker, reachable directly from the Interpreter (not from the Analyst/Examiner/Builder chain), that generates one or more runnable HTML/CSS concept directions for a described need, lets the Owner review and iterate on them, and hands the approved concept back to the Interpreter to fold into the normal `behaviour-to-implement` flow.

Review is done through a small adapter interface, not a hard dependency on any one tool — see "The review-adapter interface" below before implementing the review loop.

## New topology edge

This is the one addition to the fixed topology table from the project overview — everything else is unchanged:

```
interpreter → designer  : design-request
designer    → interpreter : design-result
```

### New message payload shapes

- `design-request`: `{ text: str }` — same shape as `behaviour-to-implement`; the Designer, like the Analyst, only ever sees the need in the Owner's own words.
- `design-result`: `{ artifact_ref: str, description: str, iterations: int }` — `artifact_ref` points at the final approved HTML file (relative to the project, so the Interpreter/Analyst can reference it later), `description` is a plain-language summary of the direction (no implementation detail beyond what's visually true), `iterations` records how many review rounds it took.

Add both to the envelope/payload validation from Phase 1 — same mechanism, just two more entries in the topology table and two more `pydantic` payload models.

## The review-adapter interface

Don't call any review tool directly from the Designer's core logic. Define one narrow interface instead:

```
review(artifact_path: str) -> "approved" | feedback_text: str
```

Everything else in this phase — worktree management, harness invocation, `design-result` assembly — depends only on this interface, never on which implementation is behind it. Ship two implementations:

- **`lavish`** — opens the artifact via `lavish` (lavish-axi — https://github.com/kunchenguid/lavish-axi), collects structured annotations, and flattens them into feedback text for the next regeneration pass.
- **`basic`** — the always-available fallback, no extra dependency. Opens the artifact in the OS default browser (`open`/`xdg-open`/equivalent), and surfaces it through the existing Interpreter conversation ("here's the direction, take a look — what do you think?"). The Owner's next chat reply *is* the feedback text; no new tool involved.

Selection: `[roles.designer].review_tool` in `config.toml`, one of `"auto" | "lavish" | "basic"`, defaulting to `"auto"` — auto-detect by checking whether `lavish`/`npx lavish-axi` resolves on `PATH` at runtime, falling back to `basic` silently if not. This is the only place in the Designer's implementation that should know Lavish exists at all.

## Deliverables

### 1. Worktree isolation, same pattern as the Builder

On receiving `design-request`, create `.relay/work/<behaviour_id>-design/` as its own git worktree/branch, separate from any worktree the Builder later creates for the same behaviour once implementation begins. Never touch the main project tree or any Builder worktree directly.

### 2. Headless harness invocation

- Read `model` (and `harness`, same field name and meaning as `[roles.builder]`) from a new `[roles.designer]` section in `config.toml`.
- Spawn the configured harness headlessly inside the Designer's worktree, with a persona prompt built from `.relay/personas/designer.md` (or the built-in default) plus the `design-request` payload's `text`.
- The persona must instruct the harness session to:
  - Produce one or more real, runnable HTML/CSS documents — never a static image, never a text description of what something would look like.
  - Never implement real backend logic, data fetching, or business logic — a Designer artifact is a visual/interaction shell only.
  - Never touch anything outside its own worktree.
  - Once a first draft exists, hand it to the configured review adapter rather than invoking any specific review tool directly.

### 3. The review loop

This is a live, synchronous interaction, unlike everything else in the pipeline — treat it that way rather than forcing it through the mailbox/ledger mechanism, regardless of which adapter is active:
- The Designer's headless session stays alive while the Owner reviews (via whichever adapter is active) and iterates in place (regenerating the HTML, re-reviewing), same worktree, same session.
- Track iteration count. Cap it at the project's existing `max_loops` value from `[project]` in `config.toml` — do not add a second, separate config knob for this; reuse the one that already exists.
- On reaching the cap without a clear approval signal, the session should present its current best state and end with an explicit request (surfaced up to the Interpreter) asking the Owner whether to proceed with it as-is or abandon the design exploration for this behaviour — never silently pick one on the Owner's behalf.

### 4. Producing `design-result`

Once the Owner signals satisfaction (an `"approved"` result from the active review adapter, or the Designer session simply being told to finalize), the wrapper code (not the model) assembles the `design-result` envelope, validates it via Phase 1's validator, and delivers it to the Interpreter's inbox — same "code owns the envelope, model owns the content" split used everywhere else in this project.

### 5. Interpreter-side handling (small addition to Phase 4's work)

- A new tool alongside `dispatch_behaviour`/`check_status` from Phase 4: `dispatch_design_request(text: str) -> { behaviour_id: str }`, and `check_design_status(behaviour_id: str) -> { status: "pending" | "done", artifact_ref, description }`, mirroring the existing pair.
- Update the Interpreter persona (`AGENTS.md`/`CLAUDE.md`) to recognize when a request is asking for creative/visual exploration rather than a concrete behaviour to implement, route it through `dispatch_design_request` instead of `dispatch_behaviour`, and — once a `design-result` comes back — fold the approved `artifact_ref` into the eventual `behaviour-to-implement` text sent to the Analyst when the Owner is ready to move from "what should this look like" to "build it."

## Out of scope for this phase

- Any change to the Analyst/Examiner/Builder worker logic itself — the only thing that changes for them is that a `behaviour-to-implement` message may now carry an `artifact_ref` in its text/context, which they can simply treat as additional input, not a new code path.
- Sentinel (separate phase document).
- Automating the "Owner is satisfied" detection with anything beyond a straightforward explicit signal — don't try to infer approval from ambiguous feedback.
- Additional review adapters beyond `lavish` and `basic` — the interface is designed to support more later, but only these two ship in this phase.

## Acceptance criteria

- [ ] A `design-request` message dispatched from a live Interpreter session results in a new Designer worktree, a headless harness session, and a real HTML file being generated.
- [ ] With `lavish` available on `PATH`, `review_tool = "auto"` correctly selects it and opens the generated file with no manual setup beyond what the persona already handles.
- [ ] With `lavish` unavailable (simulate by removing it from `PATH` or forcing `review_tool = "basic"`), the Designer falls back cleanly — the artifact opens in a browser and review happens through the Interpreter conversation, with no error and no degraded functionality beyond the review experience itself.
- [ ] At least one real feedback round trip is observed to work end to end through each adapter (you give feedback, the Designer regenerates), not just "runs without error."
- [ ] `iterations` in the final `design-result` accurately reflects how many rounds actually happened.
- [ ] The generated artifact contains no functional backend logic — spot check that it's genuinely a visual/interaction shell.
- [ ] Hitting `max_loops` without approval results in the Designer surfacing an explicit choice to the Owner via the Interpreter, rather than silently finalizing or looping forever.
- [ ] The eventual `behaviour-to-implement` message for a behaviour that went through design exploration correctly carries the approved `artifact_ref`, and the Analyst/Examiner/Builder chain proceeds without any code changes on their end.
