"""Schema-constrained model calls with bounded retry.

Shared by the role workers and the Sentinel.  Calls a model via a
`ModelRouter`, using JSON mode when the model's capability table allows it, and
retries on parse/schema failure with the specific error fed back to the model.
"""

from __future__ import annotations

import json

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
    overrides: dict | None = None,
    max_retries: int = MAX_SCHEMA_RETRIES,
    label: str = "output",
) -> dict:
    """Call *model* via *router* and return its output validated against *schema*.

    Returns the validated payload as a dict.  Raises `ModelCallError` if the
    model never produces output matching the schema within `max_retries`.
    """
    caps = resolve_capabilities(model, overrides or {})
    json_mode = caps.structured_output in ("strict", "loose")

    errors: list[str] = []
    for _ in range(max_retries):
        full_prompt = prompt
        if errors:
            full_prompt += (
                "\n\nYour previous response was invalid: "
                + "; ".join(errors)
                + "\nCorrect it and respond again with JSON matching the "
                  "required schema."
            )

        raw = router.complete(model, full_prompt, json_mode=json_mode)

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
