# Patch — Make the `strict` Capability Tier Real

Assumes current `main`. This addresses the oldest unresolved item from the original reviews: the model capability table distinguishes `strict` / `loose` / `none` structured-output support, but `call_for_schema` collapses `strict` and `loose` into the same generic JSON-mode boolean, so the `strict` tier's actual payoff — provider-native schema-constrained decoding — is never realized. This is a quality/efficiency improvement, not a bug fix: things work today, but strict-capable models (Anthropic, OpenAI) retry more than they should because they're only told "return JSON," not "return JSON matching *this* schema."

## The current state

`call_for_schema` (`model_calls.py`):

```python
caps = resolve_capabilities(model, overrides or {})
json_mode = caps.structured_output in ("strict", "loose")
```

Both `strict` and `loose` become `json_mode=True`, and `LiteLLMRouter.complete()` turns that into `response_format={"type": "json_object"}` — generic JSON mode, no schema. So the capability table's central distinction is currently cosmetic. A malformed-but-valid-JSON response (right shape of container, wrong fields) still slips through JSON mode and only gets caught by the pydantic validation retry — exactly the round-trip that real schema-constrained decoding would prevent.

## The fix

Pass the actual pydantic schema through to the provider when the model supports strict mode, using LiteLLM's `response_format` with a JSON schema (which LiteLLM translates to each provider's native mechanism — OpenAI structured outputs, Anthropic tool-use constrained decoding, etc.). Fall back to plain JSON mode for `loose`, and no `response_format` for `none`.

### 1. `complete()` accepts an optional schema

`src/brigade/llm.py` — replace the `json_mode: bool` parameter with a richer structured-output spec. To avoid churning every call site's signature, keep `json_mode` working but add an optional `schema`:

```python
def complete(
    self,
    model: str,
    prompt: str,
    role: str,
    json_mode: bool = False,
    schema: dict | None = None,   # JSON schema for strict constrained decoding
    timeout: float | None = None,
) -> str:
    ...
    if schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": schema, "strict": True},
        }
    elif json_mode:
        kwargs["response_format"] = {"type": "json_object"}
```

`schema` takes precedence over `json_mode` when both are somehow set. When `schema` is provided, the provider is constrained to emit output matching it; when only `json_mode` is set, it's the current generic-JSON behavior; when neither, no `response_format` (the `none` path).

### 2. `call_for_schema` derives the schema and branches on the tier

`src/brigade/model_calls.py`:

```python
caps = resolve_capabilities(model, overrides or {})
mode = caps.structured_output   # "strict" | "loose" | "none"

json_schema = None
json_mode = False
if mode == "strict":
    json_schema = schema.model_json_schema()   # pydantic → JSON schema
elif mode == "loose":
    json_mode = True
# mode == "none": neither — rely on prompt + parse/validate retry only
```

Then pass both through to `router.complete(...)`:

```python
raw = router.complete(
    model, full_prompt, role,
    json_mode=json_mode, schema=json_schema, timeout=timeout,
)
```

### 3. Graceful degradation when strict decoding is rejected

Not every model LiteLLM routes to will actually accept a `json_schema` response format, even if the capability table thinks its provider supports it (a newer/older model variant, an OpenRouter passthrough that doesn't forward it). If the provider rejects the request specifically because of the schema `response_format`, the call shouldn't hard-fail the behaviour — it should fall back to loose JSON mode and let the existing validation retry catch shape errors.

In `call_for_schema`, wrap the strict attempt so a `response_format`-related provider error downgrades to loose mode for the remaining retries rather than raising:

```python
try:
    raw = router.complete(model, full_prompt, role,
                          json_mode=json_mode, schema=json_schema, timeout=timeout)
except BadRequestError as exc:   # litellm.BadRequestError, lazy-imported
    if json_schema is not None and _looks_like_response_format_error(exc):
        logging.getLogger(role).warning(
            "model %s rejected strict schema decoding — falling back to JSON mode",
            model, extra={"event": "strict_fallback"},
        )
        json_schema = None
        json_mode = True
        continue   # retry this attempt in loose mode
    raise
```

`_looks_like_response_format_error` is a small heuristic on the exception message (mentions `response_format` / `json_schema` / `schema`) — keep it conservative; a genuine bad-prompt 400 should still raise, not silently downgrade. This keeps the strict path an *optimization* that never makes things worse than loose mode would have been.

### 4. Consider caching the generated schema

`schema.model_json_schema()` is cheap but called on every attempt. Hoist it above the retry loop (compute once per `call_for_schema` invocation), not inside it — trivial, just don't put it in the `for attempt` body.

## Out of scope for this patch

- Expanding the capability table beyond the current Anthropic/OpenAI/Deepseek set — strict just starts actually working for the providers already marked `strict`.
- Streaming — unchanged, still non-streaming.
- Changing what counts as `strict` vs `loose` for existing providers — the table's classifications stay as they are; this patch only makes the `strict` classification *do* something.

## Acceptance criteria

- [ ] For a `strict` model, `call_for_schema` passes a `response_format` of type `json_schema` containing the pydantic model's JSON schema — verify by inspecting the kwargs handed to a mocked `litellm.completion`.
- [ ] For a `loose` model, it passes `{"type": "json_object"}` (unchanged from today).
- [ ] For a `none` model, it passes no `response_format` at all.
- [ ] The schema is computed once per `call_for_schema` call, not once per retry attempt.
- [ ] A mocked provider that raises a `response_format`-related `BadRequestError` on a strict call causes a fallback to loose mode (logged as `strict_fallback`) and the call still succeeds on the loose retry — it does not hard-fail.
- [ ] A `BadRequestError` that is *not* about `response_format` still propagates (not silently swallowed as a fallback).
- [ ] With real Anthropic/OpenAI models, a run produces fewer schema-validation retries than before for the same behaviours — the actual payoff. (Hard to unit-test; verify by comparing retry counts in `brigade.log` across a before/after run on the same task, or at minimum confirm strict-mode calls succeed first-try more often.)
- [ ] Full existing test suite still passes — including any test that asserted the old `json_mode = strict or loose` collapse, which needs updating.
