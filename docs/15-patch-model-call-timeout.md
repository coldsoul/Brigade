# Patch — Bound Model Calls With a Timeout

Assumes the current `main` (post phase 12 + the two TUI fix commits). This is a latent-bug fix, not a UX feature — it is the root cause of a worker appearing to "hang" with no feedback: a model call that can block indefinitely.

## The bug

`LiteLLMRouter.complete()` (`src/brigade/llm.py`) calls `litellm.completion(**kwargs)` with `kwargs` containing only `model`, `messages`, and optionally `response_format`. There is **no `timeout`**. With no explicit timeout, a completion can block for a very long time (litellm's own default is on the order of minutes, and a stalled connection can exceed even that), during which the worker thread sits inside the call emitting nothing.

Observed symptom: the Analyst logs `consuming behaviour-to-implement (...)` and then nothing — no `produced`, no error, no traceback — for minutes. That's not a crash and not a dropped TUI event; it's the worker thread parked inside an unbounded blocking call. This patch makes such a call fail loudly after a bounded time instead of hanging, so the existing retry/error path can take over.

## The fix

### 1. Config — a model-call timeout with a sensible default

Add a project-level default plus optional per-role override, matching the existing `[project]` / `[roles.*]` config shape.

- `[project].model_timeout_seconds` — default `120`. Applies to any role that doesn't override it.
- `[roles.<role>].model_timeout_seconds` — optional per-role override.

In `config.py`, parse both, resolving each role's effective timeout at load time (role override if present, else project default) so the worker just reads one resolved number. Do **not** apply this to the Builder/Designer *harness* subprocess — that's a separate mechanism (a headless coding session legitimately runs much longer than a single completion) and is out of scope here. This timeout is only for the single `litellm.completion()` calls made by `call_for_schema` (Analyst, Examiner, and the Sentinel's own model call).

### 2. Thread the timeout to `complete()`

`ModelRouter`/`LiteLLMRouter.complete()` gains a `timeout: float | None = None` parameter:

```python
def complete(self, model: str, prompt: str, role: str, json_mode: bool = False,
             timeout: float | None = None) -> str:
    ...
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if timeout is not None:
        kwargs["timeout"] = timeout

    response = litellm.completion(**kwargs)
```

`litellm` passes `timeout` through to the underlying provider call and raises `litellm.Timeout` (a subclass of `litellm.exceptions.APIError`) when it's exceeded.

### 3. Thread the resolved timeout through `call_for_schema` and the workers

- `call_for_schema` (`model_calls.py`) gains a `timeout: float | None = None` parameter and passes it into `router.complete(...)`.
- The base worker's `_call_model` passes the role's resolved timeout (from config, step 1) into `call_for_schema`.
- The Sentinel's `call_for_schema` call passes its own resolved timeout (project default is fine for the Sentinel unless a `[roles.sentinel].model_timeout_seconds` is set).

### 4. Make a timeout a *retryable* failure, not a fatal one

This is the important behavioral choice. A timeout should feed the existing retry loop in `call_for_schema`, not abort the whole behaviour on the first slow call — a transient provider stall on attempt 1 may well succeed on attempt 2.

In `call_for_schema`'s retry loop, catch `litellm.Timeout` (import it lazily, alongside the existing `litellm` import, to avoid a hard import at module load) the same way parse/validation errors are already caught — record it as an error, log it, and continue to the next attempt. If all attempts time out, raise `ModelCallError` as it already does for exhausted retries, so the worker surfaces a clean `WorkerError` rather than an unhandled `litellm.Timeout` bubbling up.

Log each timeout at `WARNING` with the role and attempt number, via the role's own logger, so it's visible in `brigade.log` and (once the visibility patches land) surfaceable in the TUI:

```python
logging.getLogger(role).warning(
    "model call timed out after %ss (attempt %d/%d)", timeout, attempt, max_retries,
    extra={"event": "model_timeout"},
)
```

(The `event: "model_timeout"` extra is forward-looking — harmless now, and lets a later TUI patch show retry/timeout activity without another code change here.)

## Out of scope for this patch

- Any TUI change — this patch is purely about bounding the call and handling the timeout. Surfacing "calling model… / retrying" live in the dashboard is a separate visibility patch.
- Harness subprocess timeouts (Builder/Designer) — different mechanism, not touched here.
- Streaming completions — not introduced here; the call stays non-streaming.

## Acceptance criteria

- [ ] `[project].model_timeout_seconds` defaults to `120` when absent from `config.toml`, and a `[roles.<role>]` override takes precedence for that role — verify both with a config-parsing test.
- [ ] `LiteLLMRouter.complete()` passes `timeout` into `litellm.completion()` when set, and omits it when `None` — verify by inspecting the kwargs passed to a mocked `litellm.completion`.
- [ ] A mocked `litellm.completion` that raises `litellm.Timeout` on the first call and succeeds on the second results in `call_for_schema` returning the successful result — i.e. a timeout is retried, not fatal.
- [ ] A mocked `litellm.completion` that always raises `litellm.Timeout` results in a `ModelCallError` after `max_retries` attempts (surfaced as a `WorkerError`), not an unhandled `litellm.Timeout` — and each attempt logs a `model_timeout` warning with the correct attempt number.
- [ ] Full existing test suite still passes.
- [ ] Manual check against the real hang: run the actual feature that was hanging with a short `model_timeout_seconds` (e.g. 30) set for the analyst, and confirm that a stalled call now fails and retries (visible in `brigade.log`) rather than sitting silently — this is the real-world confirmation the code-level tests can't fully provide.
