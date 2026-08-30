"""Tests for patch 19 — strict schema-constrained decoding."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from brigade.llm import LiteLLMRouter, ModelRouter
from brigade.model_calls import call_for_schema


class _Schema(BaseModel):
    x: int


def _mock_completion(monkeypatch, content="{}"):
    """Monkeypatch litellm.completion, capturing its kwargs, returning *content*."""
    import litellm

    captured = {}

    class _Usage:
        prompt_tokens = 1
        completion_tokens = 1

    class _Msg:
        pass

    class _Choice:
        pass

    class _Response:
        pass

    msg = _Msg()
    msg.content = content
    choice = _Choice()
    choice.message = msg
    response = _Response()
    response.choices = [choice]
    response.usage = _Usage()

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(litellm, "completion", fake_completion)
    return captured


# ---------------------------------------------------------------------------
# Deliverable 1 — complete() accepts an optional schema
# ---------------------------------------------------------------------------

class TestRouterResponseFormat:
    def test_schema_produces_json_schema_response_format(self, monkeypatch):
        captured = _mock_completion(monkeypatch)
        LiteLLMRouter().complete(
            "anthropic/claude-sonnet-5", "prompt", "analyst", schema={"type": "object"}
        )
        rf = captured["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "response"
        assert rf["json_schema"]["schema"] == {"type": "object"}
        assert rf["json_schema"]["strict"] is True

    def test_json_mode_produces_json_object(self, monkeypatch):
        captured = _mock_completion(monkeypatch)
        LiteLLMRouter().complete(
            "deepseek/deepseek-v4-flash", "prompt", "analyst", json_mode=True
        )
        assert captured["response_format"] == {"type": "json_object"}

    def test_neither_omits_response_format(self, monkeypatch):
        captured = _mock_completion(monkeypatch)
        LiteLLMRouter().complete("unknown/model", "prompt", "analyst")
        assert "response_format" not in captured

    def test_schema_takes_precedence_over_json_mode(self, monkeypatch):
        captured = _mock_completion(monkeypatch)
        LiteLLMRouter().complete(
            "anthropic/claude-sonnet-5", "prompt", "analyst",
            json_mode=True, schema={"type": "object"},
        )
        assert captured["response_format"]["type"] == "json_schema"


# ---------------------------------------------------------------------------
# Deliverable 2 — call_for_schema branches on the tier
# ---------------------------------------------------------------------------

class TestCallForSchemaBranching:
    def test_strict_model_uses_json_schema(self, monkeypatch):
        captured = _mock_completion(monkeypatch, content='{"x": 1}')
        result = call_for_schema(
            LiteLLMRouter(), "anthropic/claude-sonnet-5", "prompt", _Schema, "analyst"
        )
        assert result == {"x": 1}
        assert captured["response_format"]["type"] == "json_schema"

    def test_loose_model_uses_json_object(self, monkeypatch):
        captured = _mock_completion(monkeypatch, content='{"x": 1}')
        result = call_for_schema(
            LiteLLMRouter(), "deepseek/deepseek-v4-flash", "prompt", _Schema, "analyst"
        )
        assert result == {"x": 1}
        assert captured["response_format"] == {"type": "json_object"}

    def test_none_model_omits_response_format(self, monkeypatch):
        captured = _mock_completion(monkeypatch, content='{"x": 1}')
        result = call_for_schema(
            LiteLLMRouter(), "unknown/model", "prompt", _Schema, "analyst"
        )
        assert result == {"x": 1}
        assert "response_format" not in captured


# ---------------------------------------------------------------------------
# Deliverable 4 — schema computed once, not per retry
# ---------------------------------------------------------------------------

class TestSchemaComputedOnce:
    def test_schema_object_reused_across_retries(self):
        class _RetryRouter(ModelRouter):
            def __init__(self):
                self.schemas = []

            def complete(self, model, prompt, role, json_mode=False, schema=None, timeout=None):
                self.schemas.append(schema)
                if len(self.schemas) < 3:
                    return '{"y": 1}'  # invalid — missing x
                return '{"x": 1}'

        router = _RetryRouter()
        result = call_for_schema(
            router, "anthropic/claude-sonnet-5", "prompt", _Schema, "analyst"
        )
        assert result == {"x": 1}
        assert len(router.schemas) == 3
        # same object identity across retries proves it was computed once
        assert all(s is router.schemas[0] for s in router.schemas)


# ---------------------------------------------------------------------------
# Deliverable 3 — graceful fallback when strict decoding is rejected
# ---------------------------------------------------------------------------

class TestStrictFallback:
    def test_response_format_rejection_falls_back_to_loose(self, caplog):
        import litellm

        class _FallbackRouter(ModelRouter):
            def __init__(self):
                self.calls = []

            def complete(self, model, prompt, role, json_mode=False, schema=None, timeout=None):
                self.calls.append((json_mode, schema))
                if schema is not None:
                    raise litellm.BadRequestError(
                        "response_format json_schema not supported",
                        "anthropic/claude-sonnet-5", "anthropic",
                    )
                return '{"x": 1}'

        router = _FallbackRouter()
        with caplog.at_level(logging.WARNING):
            result = call_for_schema(
                router, "anthropic/claude-sonnet-5", "prompt", _Schema, "analyst"
            )

        assert result == {"x": 1}
        # first call strict (schema set), second call loose fallback (json_mode)
        assert router.calls[0][1] is not None
        assert router.calls[0][0] is False
        assert router.calls[1][1] is None
        assert router.calls[1][0] is True
        assert any(
            getattr(r, "event", None) == "strict_fallback" for r in caplog.records
        )

    def test_non_response_format_bad_request_propagates(self):
        import litellm

        class _BadPromptRouter(ModelRouter):
            def complete(self, model, prompt, role, json_mode=False, schema=None, timeout=None):
                raise litellm.BadRequestError(
                    "prompt too long", "anthropic/claude-sonnet-5", "anthropic"
                )

        with pytest.raises(litellm.BadRequestError):
            call_for_schema(
                _BadPromptRouter(), "anthropic/claude-sonnet-5", "prompt", _Schema, "analyst"
            )
