"""Tests for patch 15 — model-call timeout."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from brigade.config import Config
from brigade.llm import ModelRouter
from brigade.model_calls import ModelCallError, call_for_schema


class _Schema(BaseModel):
    x: int


class TestConfig:
    def test_default_timeout(self):
        config = Config.model_validate({"roles": {}})
        assert config.role_timeout("analyst") == 120

    def test_role_override(self):
        config = Config.model_validate(
            {
                "project": {"model_timeout_seconds": 120},
                "roles": {"analyst": {"model_timeout_seconds": 30}},
            }
        )
        assert config.role_timeout("analyst") == 30
        assert config.role_timeout("examiner") == 120  # no override → default


class TestRouterTimeout:
    def _fake(self, monkeypatch):
        import litellm

        captured = {}

        class _Usage:
            prompt_tokens = 1
            completion_tokens = 1

        class _Msg:
            content = "hi"

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]
            usage = _Usage()

        def fake_completion(**kwargs):
            captured.update(kwargs)
            return _Response()

        monkeypatch.setattr(litellm, "completion", fake_completion)
        return captured

    def test_passes_timeout(self, monkeypatch):
        from brigade.llm import LiteLLMRouter

        captured = self._fake(monkeypatch)
        LiteLLMRouter().complete("deepseek/deepseek-v4-flash", "prompt", "analyst", timeout=30)
        assert captured.get("timeout") == 30

    def test_omits_timeout_when_none(self, monkeypatch):
        from brigade.llm import LiteLLMRouter

        captured = self._fake(monkeypatch)
        LiteLLMRouter().complete("deepseek/deepseek-v4-flash", "prompt", "analyst", timeout=None)
        assert "timeout" not in captured


class TestTimeoutRetry:
    def test_timeout_is_retried(self):
        import litellm

        calls = {"n": 0}

        class _FlakyRouter(ModelRouter):
            def complete(self, model, prompt, role, json_mode=False, schema=None, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise litellm.Timeout("simulated timeout", "deepseek/deepseek-v4-flash", "deepseek")
                return '{"x": 1}'

        result = call_for_schema(
            _FlakyRouter(), "deepseek/deepseek-v4-flash", "prompt", _Schema, "analyst", timeout=5
        )
        assert result == {"x": 1}
        assert calls["n"] == 2

    def test_all_timeouts_raise_model_call_error(self, caplog):
        import litellm

        class _AlwaysTimeoutRouter(ModelRouter):
            def complete(self, model, prompt, role, json_mode=False, schema=None, timeout=None):
                raise litellm.Timeout("simulated timeout", "deepseek/deepseek-v4-flash", "deepseek")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ModelCallError):
                call_for_schema(
                    _AlwaysTimeoutRouter(), "deepseek/deepseek-v4-flash", "prompt",
                    _Schema, "analyst", timeout=5, max_retries=3,
                )

        timeouts = [r for r in caplog.records if getattr(r, "event", None) == "model_timeout"]
        assert len(timeouts) == 3
        for i, r in enumerate(timeouts, 1):
            assert f"attempt {i}/3" in r.getMessage()
