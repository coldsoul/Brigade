"""Structured events bridging the log stream to the TUI."""

from __future__ import annotations

from typing import Literal

from textual.message import Message


class RoleEvent(Message):
    """A pipeline event attributable to a role (Analyst/Examiner/Builder/…)."""

    def __init__(
        self,
        role: str,
        event: Literal[
            "consuming",
            "produced",
            "harness_start",
            "harness_end",
            "scan_complete",
            "worker_start",
            "worker_error",
            "worker_crashed",
            "worker_exit",
        ],
        behaviour_id: str | None = None,
        message_type: str | None = None,
        message_id: str | None = None,
        to_role: str | None = None,
        loop_count: int | None = None,
        max_loops: int | None = None,
        duration_s: float | None = None,
        concern_count: int | None = None,
        text: str = "",
        level: str = "INFO",
        ts: float = 0.0,
    ):
        super().__init__()
        self.role = role
        self.event = event
        self.behaviour_id = behaviour_id
        self.message_type = message_type
        self.message_id = message_id
        self.to_role = to_role
        self.loop_count = loop_count
        self.max_loops = max_loops
        self.duration_s = duration_s
        self.concern_count = concern_count
        self.text = text
        self.level = level
        self.ts = ts


class UsageEvent(Message):
    """A token-usage record from a model call, attributed to a role."""

    def __init__(
        self,
        role: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        ts: float,
    ):
        super().__init__()
        self.role = role
        self.provider = provider
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.ts = ts
