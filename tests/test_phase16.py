"""Tests for patch 16 — fail-fast config validation."""

from __future__ import annotations

import logging

import pytest

from brigade.config import Config, ConfigError, validate_runtime_config


def _full_config() -> Config:
    return Config.model_validate(
        {
            "roles": {
                "analyst": {"model": "deepseek/deepseek-v4-flash"},
                "examiner": {"model": "deepseek/deepseek-v4-pro"},
                "builder": {"model": "deepseek/deepseek-v4-flash", "harness": "opencode"},
                "designer": {"model": "deepseek/deepseek-v4-pro", "harness": "opencode"},
                "sentinel": {"model": "deepseek/deepseek-v4-flash"},
            },
        }
    )


class TestValidateRuntimeConfig:
    def test_fully_configured_passes(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        validate_runtime_config(_full_config())  # no raise

    def test_missing_model_raises_naming_role(self):
        with pytest.raises(ConfigError) as exc:
            validate_runtime_config(Config.model_validate({"roles": {}}))
        assert "analyst" in str(exc.value)

    def test_multiple_problems_reported_together(self):
        with pytest.raises(ConfigError) as exc:
            validate_runtime_config(Config.model_validate({"roles": {}}))
        message = str(exc.value)
        for role in ("analyst", "examiner", "builder", "designer", "sentinel"):
            assert role in message

    def test_missing_api_key_names_env_var_and_role(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ConfigError) as exc:
            validate_runtime_config(_full_config())
        assert "DEEPSEEK_API_KEY" in str(exc.value)
        assert "analyst" in str(exc.value)

    def test_malformed_model_raises(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        config = Config.model_validate(
            {
                "roles": {
                    "analyst": {"model": "deepseek-v4-flash"},  # missing "/"
                    "examiner": {"model": "deepseek/deepseek-v4-pro"},
                    "builder": {"model": "deepseek/deepseek-v4-flash", "harness": "opencode"},
                    "designer": {"model": "deepseek/deepseek-v4-pro", "harness": "opencode"},
                    "sentinel": {"model": "deepseek/deepseek-v4-flash"},
                },
            }
        )
        with pytest.raises(ConfigError) as exc:
            validate_runtime_config(config)
        assert "provider/model" in str(exc.value)

    def test_unknown_provider_warns_but_does_not_block(self, monkeypatch, caplog):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        config = Config.model_validate(
            {
                "roles": {
                    "analyst": {"model": "mystery/model-x"},  # unknown provider
                    "examiner": {"model": "deepseek/deepseek-v4-pro"},
                    "builder": {"model": "deepseek/deepseek-v4-flash", "harness": "opencode"},
                    "designer": {"model": "deepseek/deepseek-v4-pro", "harness": "opencode"},
                    "sentinel": {"model": "deepseek/deepseek-v4-flash"},
                },
            }
        )
        with caplog.at_level(logging.WARNING):
            validate_runtime_config(config)  # no raise

        warnings = [r for r in caplog.records if "mystery" in r.getMessage()]
        assert len(warnings) == 1
