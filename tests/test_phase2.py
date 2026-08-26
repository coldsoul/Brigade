"""Tests for Phase 2 — config, capabilities, personas, and workers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ulid import ULID

from brigade.capabilities import resolve_capabilities
from brigade.config import Config, ConfigError, load_config
from brigade.llm import ModelRouter
from brigade.messages import Message
from brigade.personas import load_persona
from brigade.storage import (
    consume,
    deliver,
    list_inbox,
    list_ledger,
    read_message,
    write_message,
)
from brigade.workers import AnalystWorker, ExaminerWorker, WorkerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _id() -> str:
    return str(ULID())


def _make_message(
    msg_type: str,
    from_role: str,
    to_role: str,
    payload: dict,
    behaviour_id: str | None = None,
    reply_to: str | None = None,
) -> Message:
    return Message(
        id=_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=behaviour_id or _id(),
        reply_to=reply_to,
        payload=payload,
    )


def _make_brigade_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".brigade"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "interpreter"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    (d / "personas").mkdir()
    return d


def _make_config(max_loops: int = 3) -> Config:
    return Config.model_validate(
        {
            "project": {"max_loops": max_loops},
            "roles": {
                "analyst": {"model": "anthropic/claude-sonnet-5"},
                "examiner": {"model": "anthropic/claude-sonnet-5"},
                "builder": {"model": "anthropic/claude-sonnet-5", "harness": "claude"},
            },
        }
    )


class FakeRouter(ModelRouter):
    """Returns scripted responses, recording every call."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete(self, model: str, prompt: str, role: str, json_mode: bool = False) -> str:
        self.calls.append(prompt)
        if not self.responses:
            return "{}"
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestConfig:
    def test_parses_full_shape(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        (brigade_dir / "config.toml").write_text(
            """\
[project]
max_loops = 5

[roles.analyst]
model = "anthropic/claude-sonnet-5"

[roles.examiner]
model = "openrouter/anthropic/claude-sonnet-5"

[roles.builder]
harness = "opencode"
model = "openrouter/deepseek/deepseek-coder"

[capabilities.model_overrides]
"""
        )
        cfg = load_config(brigade_dir)
        assert cfg.project.max_loops == 5
        assert cfg.roles["analyst"].model == "anthropic/claude-sonnet-5"
        assert cfg.roles["builder"].harness == "opencode"
        assert cfg.capabilities.model_overrides == {}

    def test_missing_builder_harness_is_an_error(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        (brigade_dir / "config.toml").write_text(
            """\
[project]
max_loops = 3

[roles.builder]
model = "anthropic/claude-sonnet-5"
"""
        )
        with pytest.raises(ConfigError) as exc:
            load_config(brigade_dir)
        assert "harness" in str(exc.value)

    def test_missing_config_file_is_an_error(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        with pytest.raises(ConfigError):
            load_config(brigade_dir)

    def test_default_max_loops(self):
        cfg = Config.model_validate({"roles": {"builder": {"harness": "claude"}}})
        assert cfg.project.max_loops == 3


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_anthropic_is_strict(self):
        assert resolve_capabilities("anthropic/claude-sonnet-5").structured_output == "strict"

    def test_openai_is_strict(self):
        assert resolve_capabilities("openai/gpt-4o").structured_output == "strict"

    def test_deepseek_is_loose(self):
        assert resolve_capabilities("deepseek/deepseek-coder").structured_output == "loose"

    def test_unknown_provider_is_none(self):
        assert resolve_capabilities("mystery/model-x").structured_output == "none"

    def test_override_takes_precedence(self):
        caps = resolve_capabilities(
            "mystery/model-x",
            {"mystery/model-x": {"structured_output": "strict"}},
        )
        assert caps.structured_output == "strict"


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------

class TestPersonas:
    def test_builtin_persona_has_required_parts(self):
        for role in ("analyst", "examiner"):
            persona = load_persona(role)
            assert "Identity" in persona
            assert "Allowed neighbours" in persona
            assert "Forbidden leakage" in persona
            assert "Output contract" in persona

    def test_file_override(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        (brigade_dir / "personas" / "analyst.md").write_text("CUSTOM ANALYST")
        assert load_persona("analyst", brigade_dir) == "CUSTOM ANALYST"


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------

class TestAnalyst:
    def test_behaviour_to_implement_produces_behaviour(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({
                "actor": "a user",
                "outcome": "can log in",
                "boundaries": "web only",
            })
        ])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-to-implement",
            "interpreter",
            "analyst",
            {"text": "users need to log in"},
        )
        deliver(incoming, brigade_dir)

        worker.run_once()

        # behaviour delivered to examiner's inbox
        inbox = list_inbox("examiner", brigade_dir)
        assert len(inbox) == 1
        reply = consume("examiner", inbox[0], brigade_dir)
        assert reply.type == "behaviour"
        assert reply.to_role == "examiner"
        assert reply.reply_to == incoming.id
        assert reply.payload["actor"] == "a user"

    def test_prompt_includes_forbidden_leakage_rule(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({"actor": "u", "outcome": "o", "boundaries": ""})
        ])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-to-implement", "interpreter", "analyst", {"text": "x"}
        )
        worker.process(incoming)

        # the prompt the model saw must carry the leakage boundary
        assert "Forbidden leakage" in router.calls[0]
        assert "no implementation detail" in router.calls[0]

    def test_behaviour_status_is_re_authored(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({
                "behaviour_id": "B1",
                "outcome": "solved",
                "summary": "the login behaviour is now complete",
            })
        ])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-status",
            "examiner",
            "analyst",
            {
                "behaviour_id": "B1",
                "outcome": "solved",
                "summary": "expectations E1..E3 all passed",
            },
            behaviour_id="B1",
        )
        deliver(incoming, brigade_dir)

        worker.run_once()

        inbox = list_inbox("interpreter", brigade_dir)
        assert len(inbox) == 1
        reply = consume("interpreter", inbox[0], brigade_dir)
        assert reply.type == "behaviour-status"
        assert reply.to_role == "interpreter"
        assert reply.reply_to == incoming.id
        assert reply.id != incoming.id
        # re-authored, not forwarded verbatim
        assert reply.payload["summary"] != incoming.payload["summary"]
        # no expectation references leaked through
        assert "E1" not in reply.payload["summary"]


# ---------------------------------------------------------------------------
# Examiner
# ---------------------------------------------------------------------------

class TestExaminer:
    def test_behaviour_produces_expectation_with_loop_bookkeeping(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({
                "expectations": [
                    {"id": "E1", "statement": "login form exists"},
                    {"id": "E2", "statement": "login succeeds with valid creds"},
                ],
                "integration_expectation": "form submits end to end",
            })
        ])
        worker = ExaminerWorker(_make_config(max_loops=3), router, brigade_dir)

        incoming = _make_message(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "user", "outcome": "can log in", "boundaries": "web"},
        )
        deliver(incoming, brigade_dir)

        worker.run_once()

        inbox = list_inbox("builder", brigade_dir)
        assert len(inbox) == 1
        reply = consume("builder", inbox[0], brigade_dir)
        assert reply.type == "expectation"
        assert reply.payload["loop_count"] == 0
        assert reply.payload["max_loops"] == 3
        assert len(reply.payload["expectations"]) == 2

    def _place_evidence(self, brigade_dir, reply_to: Message | None = None):
        """Deliver an evidence message to the examiner's inbox."""
        evidence = _make_message(
            "evidence",
            "builder",
            "examiner",
            {
                "evidence": [
                    {
                        "expectation_id": "E1",
                        "claim": "done",
                        "execution": {
                            "command": "pytest",
                            "raw_output": "1 passed",
                            "artifact_ref": None,
                        },
                        "confidence": "executed",
                    }
                ],
                "test_files_touched": ["test.py"],
            },
            reply_to=reply_to.id if reply_to else None,
        )
        deliver(evidence, brigade_dir)
        return evidence

    def test_evidence_all_satisfied_yields_solved(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({
                "satisfied": ["E1"],
                "unmet": [],
                "summary": "the login behaviour works",
            })
        ])
        worker = ExaminerWorker(_make_config(), router, brigade_dir)

        self._place_evidence(brigade_dir)
        worker.run_once()

        inbox = list_inbox("analyst", brigade_dir)
        assert len(inbox) == 1
        reply = consume("analyst", inbox[0], brigade_dir)
        assert reply.type == "behaviour-status"
        assert reply.payload["outcome"] == "solved"

    def test_evidence_unmet_below_cap_yields_verdict(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)

        # expectation with loop_count=0
        expectation = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "x"}],
                "integration_expectation": "y",
                "loop_count": 0,
                "max_loops": 3,
            },
        )
        write_message(expectation, brigade_dir)

        router = FakeRouter([
            json.dumps({
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "not done"}],
                "summary": "still failing",
            })
        ])
        worker = ExaminerWorker(_make_config(), router, brigade_dir)

        self._place_evidence(brigade_dir, reply_to=expectation)
        worker.run_once()

        inbox = list_inbox("builder", brigade_dir)
        # the evidence pointer is gone; only the verdict pointer remains
        verdicts = [m for m in inbox]
        assert len(verdicts) == 1
        reply = consume("builder", inbox[0], brigade_dir)
        assert reply.type == "verdict"
        assert reply.payload["escalate"] is False
        assert reply.payload["loop_count"] == 1  # incremented

    def test_evidence_unmet_at_cap_yields_blocked(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)

        # previous verdict with loop_count == max_loops
        verdict = _make_message(
            "verdict",
            "examiner",
            "builder",
            {
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "still broken"}],
                "loop_count": 3,
                "escalate": False,
            },
        )
        write_message(verdict, brigade_dir)

        router = FakeRouter([
            json.dumps({
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "still broken"}],
                "summary": "cap reached",
            })
        ])
        worker = ExaminerWorker(_make_config(max_loops=3), router, brigade_dir)

        self._place_evidence(brigade_dir, reply_to=verdict)
        worker.run_once()

        inbox = list_inbox("analyst", brigade_dir)
        assert len(inbox) == 1
        reply = consume("analyst", inbox[0], brigade_dir)
        assert reply.type == "behaviour-status"
        assert reply.payload["outcome"] == "blocked"


# ---------------------------------------------------------------------------
# Schema retry
# ---------------------------------------------------------------------------

class TestSchemaRetry:
    def test_broken_then_valid_response_retries_and_succeeds(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            "not json at all {{{",
            json.dumps({"actor": "u", "outcome": "o", "boundaries": ""}),
        ])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-to-implement", "interpreter", "analyst", {"text": "x"}
        )
        reply = worker.process(incoming)

        assert reply.type == "behaviour"
        # the model was called more than once (retried after the bad parse)
        assert len(router.calls) == 2
        # the retry prompt carried the error back
        assert "invalid" in router.calls[1].lower()

    def test_persistently_broken_response_fails_loudly(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter(["garbage" for _ in range(10)])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-to-implement", "interpreter", "analyst", {"text": "x"}
        )
        with pytest.raises(WorkerError):
            worker.process(incoming)

        # nothing was delivered
        assert list_inbox("examiner", brigade_dir) == []

    def test_schema_invalid_payload_retries(self, tmp_path):
        brigade_dir = _make_brigade_dir(tmp_path)
        router = FakeRouter([
            json.dumps({"actor": "u", "outcome": "o"}),  # missing boundaries
            json.dumps({"actor": "u", "outcome": "o", "boundaries": ""}),
        ])
        worker = AnalystWorker(_make_config(), router, brigade_dir)

        incoming = _make_message(
            "behaviour-to-implement", "interpreter", "analyst", {"text": "x"}
        )
        reply = worker.process(incoming)

        assert reply.payload["boundaries"] == ""
        assert len(router.calls) == 2
