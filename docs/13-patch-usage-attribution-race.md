# Patch — Fix Usage/Cost Attribution Race

Assumes phase 12 (`902f99b`) is implemented. This is a bug fix, not new functionality — the Overview screen's COST column exists and updates, but the numbers it shows cannot currently be trusted, because usage records aren't reliably attributed to the correct role.

## The bug

`LiteLLMRouter.complete()` (`src/brigade/llm.py`) logs token usage against `logging.getLogger(model.split("/", 1)[0])` — a **provider**-named logger (e.g. `"anthropic"`), not the role that made the call. The router has no way to know which role invoked it, because `role` isn't part of its signature.

Downstream, `OverviewScreen.handle_usage_event()` (`src/brigade/tui/app.py`) has no role information on the `UsageEvent` either, so it guesses via `self._last_role` — whichever role most recently produced a `RoleEvent` anywhere in the app. Since all five workers run concurrently in separate threads, this is a real race: if two roles are active around the same time (a common, not edge-case, situation — e.g. Examiner reviewing evidence while Builder starts its next loop iteration), a usage record can be credited to the wrong role. Because the cost table accumulates (`usage[role]["prompt"] += event.prompt_tokens`), one mis-attribution doesn't just produce one wrong number — it silently corrupts that role's running total for the rest of the session.

## The fix

Stop inferring role after the fact. Thread it through explicitly, since every call site already has it.

### 1. `ModelRouter`/`LiteLLMRouter` — add `role` to the call signature

`src/brigade/llm.py`:

```python
class ModelRouter:
    def complete(self, model: str, prompt: str, role: str, json_mode: bool = False) -> str:
        raise NotImplementedError


class LiteLLMRouter(ModelRouter):
    def complete(self, model: str, prompt: str, role: str, json_mode: bool = False) -> str:
        import litellm

        silence_litellm()

        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = litellm.completion(**kwargs)
        content = response.choices[0].message.content

        usage = getattr(response, "usage", None)
        logging.getLogger(role).info(
            "usage",
            extra={
                "event": "usage",
                "model": model,
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
            },
        )

        return content or ""
```

The only change: log via `logging.getLogger(role)` instead of `logging.getLogger(model.split("/", 1)[0])`. `record.name` is now the actual role, the same way it already is for every other event type (`consuming`, `produced`, `harness_start`, `harness_end`).

### 2. Every call site — pass `role`

Find every place that currently calls `router.complete(model, prompt, ...)` (workers/base.py and any subclass that calls it directly — Analyst, Examiner, Builder, Designer, and Sentinel's own model call) and add `role=self.role` (or the equivalent already-available role string for Sentinel, which isn't a `RoleWorker` subclass). This is a mechanical, no-behavior-change addition at each site — do not touch anything else in these call sites.

### 3. `UsageEvent` — carry role, not just provider

`src/brigade/tui/events.py`:

```python
class UsageEvent(Message):
    def __init__(
        self,
        role: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        ts: float,
    ):
        super().__init__()
        self.role = role
        self.provider = provider
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.ts = ts
```

Keep `provider` (still useful for display/debugging — e.g. showing which backend a role is actually configured against) alongside the new `role` field; don't remove it.

### 4. `TUILogHandler` — pass the now-correct role through

`src/brigade/tui/bridge.py`, in the `event_type == "usage"` branch:

```python
event = UsageEvent(
    role=record.name,
    provider=getattr(record, "model", "").split("/", 1)[0] if getattr(record, "model", None) else record.name,
    model=getattr(record, "model", ""),
    prompt_tokens=getattr(record, "prompt_tokens", 0),
    completion_tokens=getattr(record, "completion_tokens", 0),
    ts=record.created,
)
```

`record.name` is now the role (per the fix above), so `provider` is derived from the `model` string itself here rather than from the logger name.

### 5. `OverviewScreen.handle_usage_event` — delete the guesswork

`src/brigade/tui/app.py`:

```python
def handle_usage_event(self, event: UsageEvent) -> None:
    role = event.role
    if role not in self.usage:
        return
    usage = self.usage[role]
    usage["model"] = event.model
    usage["prompt"] += event.prompt_tokens
    usage["completion"] += event.completion_tokens

    cost = _dollar_cost(event.model, usage["prompt"], usage["completion"])
    table = self.query_one("#roles", DataTable)
    if role in table.rows:
        table.update_cell(role, "cost", f"${cost:.4f}")
```

Remove `self._last_role` entirely from `OverviewScreen` — both its assignment in `handle_role_event` and its use here. It no longer has a purpose once `UsageEvent` carries its own role.

## Out of scope for this patch

- The stdout/stderr redirection issue (separate patch document).
- Any change to how `_dollar_cost`/`litellm.cost_per_token` computes the actual dollar figure — that logic is correct as-is; the bug is entirely about *which role's row* gets updated, not the math once attribution is right.

## Acceptance criteria

- [ ] Every existing call site that invokes `router.complete(...)` now passes `role=`, and none were missed — grep for `\.complete(` across `src/brigade/workers/` and `src/brigade/sentinel.py` to confirm.
- [ ] A usage `LogRecord`'s `record.name` is the role that made the call, verified directly (not just "the total looks about right") — write a test that calls `LiteLLMRouter.complete()` with a fake/mocked `litellm.completion` returning a known usage object, for a specific `role`, and asserts the resulting log record's `.name` equals that role.
- [ ] **The regression test for the actual bug**: with two roles' `RoleEvent`s interleaved out of order (simulate Builder producing a `RoleEvent`, then Examiner's `UsageEvent` arriving), assert that Examiner's usage lands on Examiner's row, not Builder's. This specifically must fail against the pre-fix code and pass against the fix — a test that would pass either way isn't testing the actual bug.
- [ ] `self._last_role` no longer exists anywhere in `src/brigade/tui/app.py`.
- [ ] Running a real session with two roles genuinely active concurrently (e.g. Builder mid-harness-run while Examiner is reviewing an earlier evidence submission for a different behaviour) produces cost numbers on each role's row that only ever increase from that role's own actual calls — spot-check by temporarily adding a debug log of `(role, prompt_tokens, completion_tokens)` at the point of accumulation and comparing against what each role's row shows.
