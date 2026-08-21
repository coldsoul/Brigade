# Phase 7 — Sentinel

Read `00-project-overview.md` and all prior phase documents first. The Sentinel is structurally unlike every other role built so far: it doesn't consume from an inbox as part of the linear chain, it periodically audits the entire ledger across all behaviours, and — eventually — it's the one role allowed to break the neighbour rule on purpose. Build this last, and build it conservatively: start with visibility only, not autonomous intervention.

## Goal

A periodic auditor that scans `.relay/ledger/` for contract violations — leaked implementation detail, expectations quietly reworded to match convenient evidence, evidence whose confidence tier doesn't match its actual content, and systemic loop trouble — and surfaces what it finds. For this phase, it only ever advises or warns; it does not act.

## New topology additions

Unlike every other role, the Sentinel's edges are wildcards rather than fixed pairs — it can address any role, though for this phase it should only ever *write*, never receive:

```
sentinel → (any role) : advisory, warning
```

`directive` (the Sentinel injecting a corrective instruction directly into another role's inbox) is deliberately **not implemented in this phase** — see "Out of scope" below.

### New message payload shape

- `advisory` / `warning`: `{ concerns: [{ behaviour_id: str, message_id: str, category: str, description: str }], severity: "advisory" | "warning" }`. `category` should be one of a small fixed set (e.g. `"leakage"`, `"gamed_expectation"`, `"confidence_mismatch"`, `"systemic_loop"`) rather than freeform, so results are filterable later.

## Deliverables

### 1. Read-only ledger scanner

- `scan_ledger(since: str | None) -> list[Message]`: reads all ledger entries, optionally only those newer than a given `id` (ULIDs sort chronologically, so this is a cheap cursor). The Sentinel should never write to or delete anything under `.relay/ledger/mailboxes/` — it only reads the ledger and writes new `advisory`/`warning` entries of its own.
- Run as its own async task inside `relay up` (Phase 0/2's worker infrastructure), on a cadence — "every N new ledger messages since the last scan" is a better fit for this file-based design than a wall-clock timer; make N configurable in `config.toml` under a new `[roles.sentinel]` section, with a sensible default (e.g. every 10 new messages).

### 2. The four checks

Each check is its own function, taking a batch of new ledger messages plus enough surrounding context (e.g. the full history for a given `behaviour_id`) to do its job:

- **Leakage check**: for `behaviour`, `behaviour-status`, and `evidence.claim` fields specifically, look for implementation-shaped language — code identifiers, file extensions, language keywords, data-structure names — that shouldn't have survived the sending role's own forbidden-leakage instructions. A model call reading the content adversarially (matching the audit approach from the EDD article) is likely more reliable here than pure regex/heuristics, but a cheap heuristic pre-filter to avoid calling a model on every message is a reasonable optimization.
- **Gamed-expectation check**: for a given `behaviour_id`, diff consecutive `expectation` messages across loop iterations. Flag cases where an expectation's wording shifted in a way that suspiciously tracks the specific content of the prior `evidence.claim` for that same expectation — i.e. the bar moved to match what was already delivered, rather than evidence being produced to meet a fixed bar.
- **Confidence-mismatch check**: flag `evidence` entries marked `"executed"` whose `execution.raw_output` is empty, trivially short, or doesn't plausibly correspond to `execution.command`. Also flag a Builder (or Designer) that *never* uses `"executed"` across many behaviours — a sign it's avoiding scrutiny rather than a sign of genuine uncertainty every time.
- **Systemic-loop check**: across many behaviours, is a specific role hitting `max_loops` unusually often? This one doesn't produce a per-message flag — it produces a rollup observation (e.g. "the Builder has hit max_loops on 6 of the last 10 behaviours") worth surfacing as a single `advisory` about the role's configured model, not the individual behaviours.

### 3. Surfacing results

- Every `advisory`/`warning` the Sentinel produces is written to the ledger like any other message (full audit trail, no special-casing).
- Extend `relay status` (stubbed in Phase 0) to show a summary of open Sentinel flags — count by severity and category is enough for this phase, no need for a full TUI.
- No message is ever delivered into another role's inbox in this phase — `advisory`/`warning` are informational only, visible via `relay status` and the ledger itself, and require you to act on them manually if you choose to.

### 4. Model configuration

- `[roles.sentinel]` in `config.toml` gets its own `model`, same `provider/model` shape as every other role.
- Document (in the persona/README, not enforced in code for this phase) the recommendation to configure the Sentinel to a *different* model than whichever one dominates the rest of the pipeline, for the same reason the Examiner should differ from the Builder — an auditor sharing blind spots with what it's auditing is a weaker auditor.

## Out of scope for this phase

- `directive` messages — the Sentinel writing directly into another role's inbox to force a redo. This is real autonomous intervention outside the normal topology and deserves its own later phase once `advisory`/`warning` have been observed to be reliable in practice, not implemented speculatively now.
- Any change to Analyst/Examiner/Builder/Designer worker logic to *react* to Sentinel output automatically — for this phase, a human reads the flags and decides what, if anything, to do.
- Real-time/per-message auditing — the periodic batch-scan approach is intentional, to keep the Sentinel off the pipeline's critical path.

## Acceptance criteria

- [ ] `relay up` runs the Sentinel as an additional async task with no change to how the existing roles' workers behave.
- [ ] Running the Sentinel against a ledger from an earlier real dry run (Phase 5) with no injected violations produces zero or near-zero false-positive flags — verify by hand-reviewing what it flags, if anything.
- [ ] Deliberately crafting a `behaviour` message with an obvious implementation-detail leak (hand-place it in the ledger, bypassing the Analyst) is caught by the leakage check on the next scan.
- [ ] Deliberately crafting two consecutive `expectation` messages for the same `behaviour_id` where the second's wording conveniently narrows to match a prior `evidence.claim` is caught by the gamed-expectation check.
- [ ] An `evidence` message marked `"executed"` with an empty `raw_output` is caught by the confidence-mismatch check.
- [ ] Simulating several behaviours that all hit `max_loops` for the same role produces a single systemic-loop `advisory`, not N separate per-behaviour flags.
- [ ] `relay status` shows an accurate summary of open flags by severity/category after a scan.
- [ ] No `directive`-type message exists anywhere in the codebase for this phase — confirm the schema only allows `advisory`/`warning` as valid Sentinel-originated types for now.
