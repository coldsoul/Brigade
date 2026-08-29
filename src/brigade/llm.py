"""Model routing via LiteLLM.

The router is an injectable dependency so workers can be tested with a fake
router without touching the network.  LiteLLM is imported lazily so the package
imports fast and tests never load it.
"""

from __future__ import annotations

import logging

from brigade.logging_config import silence_litellm


class ModelRouter:
    """Abstract model router — returns raw model text output."""

    def complete(
        self,
        model: str,
        prompt: str,
        role: str,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        raise NotImplementedError


class LiteLLMRouter(ModelRouter):
    """Routes `provider/model` strings through LiteLLM."""

    def complete(
        self,
        model: str,
        prompt: str,
        role: str,
        json_mode: bool = False,
        timeout: float | None = None,
    ) -> str:
        import litellm  # lazy import

        # litellm attaches its own stderr StreamHandler on import — drop it so
        # its logs route through our handlers instead of drawing over the TUI.
        silence_litellm()

        kwargs: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if timeout is not None:
            kwargs["timeout"] = timeout

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
