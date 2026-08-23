"""Model routing via LiteLLM.

The router is an injectable dependency so workers can be tested with a fake
router without touching the network.  LiteLLM is imported lazily so the package
imports fast and tests never load it.
"""

from __future__ import annotations


class ModelRouter:
    """Abstract model router — returns raw model text output."""

    def complete(self, model: str, prompt: str, json_mode: bool = False) -> str:
        raise NotImplementedError


class LiteLLMRouter(ModelRouter):
    """Routes `provider/model` strings through LiteLLM."""

    def complete(self, model: str, prompt: str, json_mode: bool = False) -> str:
        import litellm  # lazy import

        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = litellm.completion(**kwargs)
        content = response.choices[0].message.content
        return content or ""
