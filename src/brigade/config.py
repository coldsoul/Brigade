"""Config parsing for `.brigade/config.toml`."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class ProjectConfig(BaseModel):
    max_loops: int = 3


class RoleConfig(BaseModel):
    model: str | None = None
    harness: str | None = None
    review_tool: str = "auto"  # designer-only: "auto" | "lavish" | "basic"
    scan_every: int = 10  # sentinel-only: scan after N new ledger messages


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
