"""Schema-constrained model calls with bounded retry.

Shared by the role workers and the Sentinel.  Calls a model via a
`ModelRouter`, using JSON mode when the model's capability table allows it, and
retries on parse/schema failure with the specific error fed back to the model.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError as PydanticValidationError

from brigade.capabilities import resolve_capabilities

MAX_SCHEMA_RETRIES = 3


class ModelCallError(Exception):
    """Raised when a model cannot produce valid structured output within retries."""


def call_for_schema(
    router,
    model: str,
    prompt: str,
    schema: type[BaseModel],
    role: str,
    overrides: dict | None = None,
    max_retries: int = MAX_SCHEMA_RETRIES,
    label: str = "output",
    timeout: float | None = None,
) -> dict:
    """Call *model* via *router* and return its output validated against *schema*.

    Returns the validated payload as a dict.  Raises `ModelCallError` if the
    model never produces output matching the schema within `max_retries`.
    """
    caps = resolve_capabilities(model, overrides or {})
    mode = caps.structured_output  # "strict" | "loose" | "none"

    json_schema = None
    json_mode = False
    if mode == "strict":
        json_schema = schema.model_json_schema()  # computed once, above the loop
    elif mode == "loose":
        json_mode = True
    # mode == "none": neither — rely on prompt + parse/validate retry only

    from litellm import BadRequestError as LitellmBadRequestError  # lazy import
    from litellm import Timeout as LitellmTimeout  # lazy import

    errors: list[str] = []
    for attempt in range(1, max_retries + 1):
        full_prompt = prompt
        if errors:
            full_prompt += (
                "\n\nYour previous response was invalid: "
                + "; ".join(errors)
                + "\nCorrect it and respond again with JSON matching the "
                  "required schema."
            )

        try:
            raw = router.complete(
                model, full_prompt, role,
                json_mode=json_mode, schema=json_schema, timeout=timeout,
            )
        except LitellmTimeout:
            logging.getLogger(role).warning(
                "model call timed out after %ss (attempt %d/%d)",
                timeout, attempt, max_retries,
                extra={"event": "model_timeout"},
            )
            errors.append(f"timed out after {timeout}s")
            continue
        except LitellmBadRequestError as exc:
            if json_schema is not None and _looks_like_response_format_error(exc):
                logging.getLogger(role).warning(
                    "model %s rejected strict schema decoding - falling back to JSON mode",
                    model, extra={"event": "strict_fallback"},
                )
                json_schema = None
                json_mode = True
                continue
            raise

        try:
            data = _parse_json(raw)
            return schema.model_validate(data).model_dump()
        except (ValueError, PydanticValidationError) as exc:
            errors.append(_format_error(exc))

    raise ModelCallError(
        f"Failed to produce a valid {label} after {max_retries} attempts: "
        + "; ".join(errors)
    )


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    # strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _format_error(exc: Exception) -> str:
    if isinstance(exc, PydanticValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        )
    return str(exc)


def _looks_like_response_format_error(exc: Exception) -> bool:
    """Return True if *exc* looks like the provider rejected our response_format.

    Kept conservative — a genuine bad-prompt 400 should still raise, not silently
    downgrade — so this only matches errors that name the response_format.
    """
    message = str(exc).lower()
    return (
        "response_format" in message
        or "json_schema" in message
        or "schema" in message
    )
