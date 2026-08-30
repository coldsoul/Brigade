"""Config parsing for `.brigade/config.toml`."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from brigade.capabilities import PROVIDER_ENV_VAR


class ProjectConfig(BaseModel):
    max_loops: int = 3
    model_timeout_seconds: int = 120


class RoleConfig(BaseModel):
    model: str | None = None
    harness: str | None = None
    review_tool: str = "auto"  # designer-only: "auto" | "lavish" | "basic"
    scan_every: int = 10  # sentinel-only: scan after N new ledger messages
    model_timeout_seconds: int | None = None  # optional per-role override


class CapabilitiesConfig(BaseModel):
    model_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


class Config(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)

    @model_validator(mode="after")
    def _require_builder_harness(self) -> "Config":
        builder = self.roles.get("builder")
        if builder is not None and builder.harness is None:
            raise ValueError(
                "[roles.builder].harness is required when [roles.builder] is "
                "configured — set it to 'claude' or 'opencode'"
            )
        return self

    def role_timeout(self, role: str) -> int:
        """Resolve *role*'s effective model-call timeout in seconds.

        A `[roles.<role>].model_timeout_seconds` override wins; otherwise the
        `[project].model_timeout_seconds` default applies.
        """
        role_cfg = self.roles.get(role)
        if role_cfg is not None and role_cfg.model_timeout_seconds is not None:
            return role_cfg.model_timeout_seconds
        return self.project.model_timeout_seconds


class ConfigError(Exception):
    """Raised when `.brigade/config.toml` is missing or malformed."""


def load_config(brigade_dir: Path) -> Config:
    path = brigade_dir / "config.toml"
    if not path.is_file():
        raise ConfigError(f"No config.toml found at {path}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Malformed config.toml: {exc}") from exc

    try:
        return Config.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Invalid config.toml: {exc}") from exc


def validate_runtime_config(config: Config) -> None:
    """Check every role brigade will actually run is ready to make model calls.

    Raises `ConfigError` (with all problems collected, not just the first) if any
    role has no model, a malformed model string, or a known provider whose API
    key is missing from the environment.  Warnings (unknown provider) are logged
    but do not raise.
    """
    problems: list[str] = []
    warnings: list[str] = []

    # The roles that make model calls and must be configured to run.
    # Interpreter is the harness itself (not a brigade worker) — excluded.
    required_roles = ["analyst", "examiner", "builder", "designer", "sentinel"]

    for role in required_roles:
        role_cfg = config.roles.get(role)
        model = role_cfg.model if role_cfg else None

        if not model:
            problems.append(f"{role}: no model configured in [roles.{role}]")
            continue

        if "/" not in model:
            problems.append(
                f"{role}: model {model!r} is not in 'provider/model' form "
                f"(e.g. 'anthropic/claude-sonnet-5')"
            )
            continue

        provider = model.split("/", 1)[0]
        env_var = PROVIDER_ENV_VAR.get(provider)
        if env_var is None:
            warnings.append(
                f"{role}: provider {provider!r} is not one brigade knows the key "
                f"convention for — can't verify its API key is set"
            )
        elif not os.environ.get(env_var):
            problems.append(
                f"{role}: {env_var} is not set (required for model {model})"
            )

    for w in warnings:
        logging.getLogger("brigade").warning(w)

    if problems:
        raise ConfigError(
            "Configuration problems prevent startup:\n  - "
            + "\n  - ".join(problems)
        )
