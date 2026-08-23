"""Built-in model capability table.

Covers Anthropic, OpenAI, and Deepseek models only.  Unknown providers fall
back to the conservative "assume nothing" default.  `config.toml`'s
`[capabilities.model_overrides]` section is the escape hatch for filling in
missing or incorrect entries by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StructuredOutput = Literal["strict", "loose", "none"]


@dataclass(frozen=True)
class ModelCapabilities:
    """What a given model is known to support."""

    structured_output: StructuredOutput = "none"


# provider → default capabilities for that provider's models
BUILTIN_CAPABILITIES: dict[str, ModelCapabilities] = {
    "anthropic": ModelCapabilities(structured_output="strict"),
    "openai": ModelCapabilities(structured_output="strict"),
    "deepseek": ModelCapabilities(structured_output="loose"),
}


def _split_provider(model: str) -> str:
    return model.split("/", 1)[0]


def resolve_capabilities(
    model: str, overrides: dict[str, dict[str, str]] | None = None
) -> ModelCapabilities:
    """Resolve a model's capabilities, consulting overrides then the built-in table.

    Override keys may be an exact model string or a provider prefix (e.g.
    `"deepseek"`).  Values are capability dicts, e.g. `{"structured_output": "strict"}`.
    """
    overrides = overrides or {}

    # 1. exact match
    if model in overrides:
        return ModelCapabilities(structured_output=_parse_mode(overrides[model]))

    # 2. provider-prefix match (longest prefix wins)
    provider = _split_provider(model)
    matching = [k for k in overrides if model.startswith(k.rstrip("/") + "/")]
    if matching:
        best = max(matching, key=len)
        return ModelCapabilities(structured_output=_parse_mode(overrides[best]))

    # 3. built-in table
    if provider in BUILTIN_CAPABILITIES:
        return BUILTIN_CAPABILITIES[provider]

    # 4. unknown — assume nothing, use the safe/slow path
    return ModelCapabilities(structured_output="none")


def _parse_mode(caps: dict[str, str]) -> StructuredOutput:
    mode = caps.get("structured_output", "none")
    if mode not in ("strict", "loose", "none"):
        return "none"
    return mode  # type: ignore[return-value]
