# Phase — TUI Dashboard

This is a patch-style phase, like `11-patch-interpreter-scope-classification.md` — it assumes the full pipeline (phases 0–4, 6, 7) and logging phases 1–4 are already implemented and working. It adds a live Textual dashboard to `brigade up`, built entirely on top of the existing `logging` infrastructure via a custom handler — no parallel event system, and no change to how workers do their actual work.

Per current direction, this phase does not worry about `brigade up` running headless, under `nohup`, or under a process supervisor — `brigade up` simply launches the dashboard. Revisit if that ever becomes a real need.

## Goal

`brigade up` renders a live terminal dashboard with four screens (Overview, Behaviours, Ledger, Flags), fed by a `logging.Handler` attached to the existing per-role loggers. Worker threads are otherwise untouched — they already log everything the dashboard needs; this phase mostly widens what goes into their existing `extra={}` calls.

## Deliverables

### 0. Usage capture (small fix to `llm.py`)

`LiteLLMRouter.complete()` currently reads only `response.choices[0].message.content` and discards `response.usage`. Capture it and log a usage record:

```python
usage = getattr(response, "usage", None)
logging.getLogger(model.split("/", 1)[0]).info(
    "usage", extra={
        "event": "usage",
        "model": model,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "completion_tokens": getattr(usage, "completion_tokens", 0),
    },
)
```

Log it against the *provider* (`model.split("/", 1)[0]`), not a role — `ModelRouter`/`LiteLLMRouter` don't know which role is calling them, and shouldn't need to; the caller's own log lines already carry `behaviour_id`/role context via their own logger. The dashboard correlates usage to a role by matching `record.name` on the worker's own logger, which each worker already logs to right before/after calling the router — no threading of role identity through the router itself.

Dollar cost is never stored — only `prompt_tokens`/`completion_tokens` are logged. The dashboard computes `$` on read via `litellm.completion_cost()`, per the earlier design decision to keep pricing out of anything that needs maintaining.

### 1. Widen existing structured log calls (`workers/base.py`, `sentinel.py`)

No new logging mechanism — extend the `extra={}` dicts already present at each call site, and add one genuinely new call site. Every field below is optional/`None` when not applicable; nothing here changes existing log line text.

**`_dispatch()`, the "consuming" call** — add:
```python
"event": "consuming",
"message_type": msg.type,
"message_id": msg.id,
"loop_count": msg.payload.get("loop_count"),
"max_loops": msg.payload.get("max_loops"),
```

**`_dispatch()`, the "produced" call** — add:
```python
"event": "produced",
"message_type": reply.type,
"message_id": reply.id,
"to_role": reply.to_role,
```

**New call, at the start of harness invocation** (wherever `_run_harness`/`_log_harness` currently begins — there is currently no "started" log, only "exited"):
```python
self.logger.info(
    "harness starting",
    extra={"behaviour_id": behaviour_id, "event": "harness_start"},
)
```

**`_log_harness()`, the "harness exited" call** — add:
```python
"event": "harness_end",
"duration_s": duration,
```

**Sentinel, on scan completion** — add a summary log carrying `event: "scan_complete"` and `concern_count` (however many `advisory`/`warning` messages the scan produced), so the Overview table's Sentinel row can show a live count without the dashboard re-running `sentinel_summary()` on its own timer.

### 2. `RoleEvent` / `UsageEvent` and the bridge handler

New module, e.g. `src/brigade/tui/events.py`:

```python
from textual.message import Message
from typing import Literal

class RoleEvent(Message):
    role: str
    event: Literal["consuming", "produced", "harness_start", "harness_end", "scan_complete"]
    behaviour_id: str | None = None
    message_type: str | None = None
    message_id: str | None = None
    to_role: str | None = None
    loop_count: int | None = None
    max_loops: int | None = None
    duration_s: float | None = None
    concern_count: int | None = None
    text: str = ""
    level: str = "INFO"
    ts: float = 0.0

class UsageEvent(Message):
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    ts: float
```

And the handler, e.g. `src/brigade/tui/bridge.py`:

```python
import logging

class TUILogHandler(logging.Handler):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        event_type = getattr(record, "event", None)
        if event_type == "usage":
            event = UsageEvent(
                provider=record.name,
                model=getattr(record, "model", ""),
                prompt_tokens=getattr(record, "prompt_tokens", 0),
                completion_tokens=getattr(record, "completion_tokens", 0),
                ts=record.created,
            )
        else:
            event = RoleEvent(
                role=record.name,
                event=event_type or "consuming",
                behaviour_id=getattr(record, "behaviour_id", None),
                message_type=getattr(record, "message_type", None),
                message_id=getattr(record, "message_id", None),
                to_role=getattr(record, "to_role", None),
                loop_count=getattr(record, "loop_count", None),
                max_loops=getattr(record, "max_loops", None),
                duration_s=getattr(record, "duration_s", None),
                concern_count=getattr(record, "concern_count", None),
                text=record.getMessage(),
                level=record.levelname,
                ts=record.created,
            )
        self.app.call_from_thread(self.app.post_message, event)
```

Attach this to the root logger immediately after `configure_logging()` runs inside `up`, only when the dashboard is actually starting. The existing console/file handlers from logging phases 1–4 stay exactly as they are — this is an additional handler, not a replacement.

### 3. The four screens

- **Overview** (default, no keybinding needed to reach it) — the fixed pipeline diagram (topology never changes, so this is a static Rich renderable) plus a `DataTable` keyed by role, one row per role, columns `ROLE / STATUS / BEHAVIOUR / COST`. Seed the table on mount from one `list_inbox()` call per role (per the earlier decision — never start from an empty table). `on_role_event` updates the relevant row by key; `event: "produced"` additionally flashes the corresponding pipeline edge (heavier line weight, not color alone, so it still reads on limited-color terminals) briefly before returning to steady state. `on_usage_event` accumulates `prompt_tokens`/`completion_tokens` per role (matched by which role's own `RoleEvent`s most recently preceded it) and recomputes the cost cell via `litellm.completion_cost()`.
- **Behaviours** (`b`) — a `ListView`, most-recent-activity-first, populated by a new `summarize_behaviours(brigade_dir)` helper that groups `list_ledger()` by `behaviour_id` and derives current role/status/loop-position/last-activity-time per group. Refreshed on a 2-second `set_interval`, not event-driven — a cheap periodic ledger scan is simpler than trying to maintain this incrementally from the event stream, and sub-second latency doesn't matter here. `enter` on a row pushes the Ledger screen for that `behaviour_id`.
- **Ledger** (pushed from Behaviours, or directly with a `behaviour_id`) — a `RichLog`, seeded on mount from `list_ledger(behaviour_id=...)`, appended to via a 1-second interval re-check for new entries (a full ledger scan is cheap; no need to wire this one through the event bus either, since it's about historical record, not live worker state). Each evidence line surfaces the `harness-<role>-<ulid>.log` path from the logging Phase 3 work, with an `[o]` binding to open it.
- **Flags** (`f`) — a list backed by the same `sentinel_summary()` function `brigade status` already uses (don't duplicate flag-counting logic). Distinguish `advisory` vs `warning` visually (color + marker shape, not just a text label). Rollup-type concerns (no single `behaviour_id`, e.g. the systemic-loop check) render as a distinct row type without an `enter`-to-ledger action, since there's nowhere for that action to go.

### 4. `brigade up` integration

Replace the current `time.sleep(1)` loop in the `up` command with launching the Textual `App` in the main thread. Worker threads are started exactly as they are today, before the app runs. `Ctrl+C`/`q` should stop the app and the process the same way `Ctrl+C` does today.

## Out of scope for this phase

- Headless/piped/`nohup`/supervised execution of `brigade up` — not handled, not falling back to anything, per current direction. If this becomes a real need later, it's a separate phase (likely a `brigade attach` command talking to a small local socket the `up` process exposes, or falling back to the JSON structured logging shelved from the logging review — either way, not now).
- Any change to worker *behavior* — every change in this phase is either a logging call or a purely additive UI layer reading those calls.
- Historical/cross-session dashboards — this phase covers the live view for the currently-running `brigade up` process only.

## Acceptance criteria

- [ ] `LiteLLMRouter.complete()` logs a `usage` event with real `prompt_tokens`/`completion_tokens` for at least one real model call — verify against the actual response object, not a stub.
- [ ] Each of the four widened log call sites (`consuming`, `produced`, the new `harness_start`, `harness_end`) carries the documented `extra` fields — verify by inspecting `LogRecord.__dict__` in a test, not just that the line text looks right.
- [ ] `TUILogHandler` correctly converts a `LogRecord` with `event="usage"` into a `UsageEvent`, and every other `event` value into a `RoleEvent`, preserving all fields.
- [ ] Starting `brigade up` in a real terminal shows the Overview screen immediately, with every role's row already populated (not blank) before any message has moved.
- [ ] Dispatching a real behaviour through a live Interpreter session while `brigade up` is running visibly updates the Overview table's role rows and flashes the correct pipeline edge, with no perceptible lag beyond normal terminal redraw.
- [ ] The Overview table's cost column increases as real model calls happen, and the number is derived from `litellm.completion_cost()` at render time — not a value computed once and cached from stale pricing.
- [ ] `b` opens the Behaviours screen showing every behaviour with ledger activity, most-recent first, correctly distinguishing in-progress / blocked / solved.
- [ ] Selecting a behaviour and pressing `enter` opens the Ledger screen scoped to only that `behaviour_id`, matching what `list_ledger(behaviour_id=...)` returns directly.
- [ ] `o` on an evidence line with a harness log reference actually opens the correct `.brigade/logs/harness-*.log` file.
- [ ] `f` opens the Flags screen showing the same counts `brigade status` reports for the same project state — no drift between the two.
- [ ] A rollup-type Sentinel concern (systemic-loop) renders without a broken or misleading `enter` action.
- [ ] `Ctrl+C` (or `q`) cleanly stops both the dashboard and the underlying worker threads — no orphaned process left behind.
