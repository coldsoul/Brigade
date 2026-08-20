"""Tests for Phase 4 — Interpreter MCP server and tool logic."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from relay.config import Config
from relay.interpreter import (
    CONVERSATION_DIRECTION,
    check_status_impl,
    dispatch_behaviour_impl,
    log_conversation_impl,
)
from relay.messages import Message
from relay.storage import deliver, list_inbox, list_ledger, read_message
from relay.workers import AnalystWorker, BuilderWorker, ExaminerWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _id() -> str:
    return str(ULID())


def _make_relay_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".relay"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "interpreter"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    (d / "personas").mkdir()
    return d


def _make_status(behaviour_id: str, outcome: str, summary: str) -> Message:
    return Message(
        id=_id(),
        type="behaviour-status",
        from_role="analyst",
        to_role="interpreter",
        behaviour_id=behaviour_id,
        payload={"behaviour_id": behaviour_id, "outcome": outcome, "summary": summary},
    )


# ---------------------------------------------------------------------------
# dispatch_behaviour
# ---------------------------------------------------------------------------

class TestDispatchBehaviour:
    def test_dispatch_delivers_behaviour_to_implement(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        result = dispatch_behaviour_impl(relay_dir, "users need to log in")

        inbox = list_inbox("analyst", relay_dir)
        assert len(inbox) == 1
        msg = read_message(inbox[0], relay_dir)
        assert msg.type == "behaviour-to-implement"
        assert msg.from_role == "interpreter"
        assert msg.to_role == "analyst"
        assert msg.payload["text"] == "users need to log in"
        assert msg.behaviour_id == result["behaviour_id"]

    def test_dispatch_returns_immediately(self, tmp_path):
        # the function must not block — it returns a dict right away
        relay_dir = _make_relay_dir(tmp_path)
        result = dispatch_behaviour_impl(relay_dir, "do a thing")
        assert isinstance(result, dict)
        assert "behaviour_id" in result


# ---------------------------------------------------------------------------
# check_status
# ---------------------------------------------------------------------------

class TestCheckStatus:
    def test_pending_when_no_status(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        assert check_status_impl(relay_dir, "nope") == {
            "outcome": "pending",
            "summary": None,
        }

    def test_solved_status_is_consumed(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        bid = _id()
        status = _make_status(bid, "solved", "the login behaviour is complete")
        deliver(status, relay_dir)

        result = check_status_impl(relay_dir, bid)
        assert result == {"outcome": "solved", "summary": "the login behaviour is complete"}

        # pointer consumed, ledger entry remains
        assert list_inbox("interpreter", relay_dir) == []
        assert read_message(status.id, relay_dir).payload["outcome"] == "solved"

    def test_blocked_status(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        bid = _id()
        deliver(_make_status(bid, "blocked", "loop cap reached"), relay_dir)
        assert check_status_impl(relay_dir, bid)["outcome"] == "blocked"

    def test_ignores_status_for_other_behaviour(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        deliver(_make_status(_id(), "solved", "other"), relay_dir)
        result = check_status_impl(relay_dir, "different-id")
        assert result["outcome"] == "pending"
        # the unrelated status was not consumed
        assert len(list_inbox("interpreter", relay_dir)) == 1


# ---------------------------------------------------------------------------
# log_conversation
# ---------------------------------------------------------------------------

class TestLogConversation:
    @pytest.mark.parametrize(
        "msg_type, expected_from, expected_to",
        [
            ("problem", "owner", "interpreter"),
            ("clarification", "interpreter", "owner"),
            ("roadmap", "interpreter", "owner"),
            ("roadmap-verdict", "owner", "interpreter"),
            ("increment", "interpreter", "owner"),
            ("continue-query", "interpreter", "owner"),
            ("feedback", "owner", "interpreter"),
            ("result", "interpreter", "owner"),
            ("question", "interpreter", "owner"),
        ],
    )
    def test_direction_inference(self, msg_type, expected_from, expected_to):
        assert CONVERSATION_DIRECTION[msg_type] == (expected_from, expected_to)

    def test_log_writes_to_ledger_not_inbox(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        mid = log_conversation_impl(relay_dir, "problem", "I need a login form")

        msg = read_message(mid, relay_dir)
        assert msg.type == "problem"
        assert msg.from_role == "owner"
        assert msg.to_role == "interpreter"
        assert msg.payload["text"] == "I need a login form"

        # never touches an inbox
        for role in ("analyst", "examiner", "builder", "interpreter"):
            assert list_inbox(role, relay_dir) == []

    def test_reply_to_chaining(self, tmp_path):
        relay_dir = _make_relay_dir(tmp_path)
        first = log_conversation_impl(relay_dir, "problem", "need a thing")
        second = log_conversation_impl(relay_dir, "roadmap", "two increments", reply_to=first)
        assert read_message(second, relay_dir).reply_to == first


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------

class TestMCPServer:
    def test_three_tools_registered(self):
        from relay.mcp_server import mcp

        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert names == {"dispatch_behaviour", "check_status", "log_conversation"}


# ---------------------------------------------------------------------------
# End-to-end: dispatch through the whole pipeline
# ---------------------------------------------------------------------------

class FakeRouter:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, model, prompt, json_mode=False):
        return self.responses.pop(0)


class FakeHarness:
    def run(self, harness, model, workdir, prompt):
        import re
        from relay.harness import HarnessResult

        evidence = {
            "evidence": [
                {
                    "expectation_id": "E1",
                    "claim": "the login behaviour works",
                    "execution": {
                        "command": "python -m pytest",
                        "raw_output": "1 passed",
                        "artifact_ref": None,
                    },
                    "confidence": "executed",
                }
            ],
            "test_files_touched": ["tests/test_login.py"],
        }
        m = re.search(r"this exact path:\n(\S+)", prompt)
        Path(m.group(1)).parent.mkdir(parents=True, exist_ok=True)
        Path(m.group(1)).write_text(json.dumps(evidence))
        return HarnessResult(command=["fake"], exit_code=0, stdout="ok", stderr="")


class TestEndToEnd:
    @pytest.fixture
    def env(self, tmp_path: Path):
        project_root = tmp_path / "project"
        project_root.mkdir()
        subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
        (project_root / "README.md").write_text("# p\n")
        subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
            cwd=project_root, check=True, capture_output=True,
        )
        relay_dir = project_root / ".relay"
        (relay_dir / "ledger").mkdir(parents=True)
        for role in ("analyst", "examiner", "builder", "interpreter"):
            (relay_dir / "mailboxes" / role / "inbox").mkdir(parents=True)
        (relay_dir / "personas").mkdir()

        config = Config.model_validate(
            {
                "project": {"max_loops": 3},
                "roles": {
                    "analyst": {"model": "anthropic/claude-sonnet-5"},
                    "examiner": {"model": "anthropic/claude-sonnet-5"},
                    "builder": {"model": "anthropic/claude-sonnet-5", "harness": "claude"},
                },
            }
        )
        return relay_dir, config

    def test_behaviour_flows_down_and_back(self, env):
        relay_dir, config = env

        # 1. Owner's problem is logged, then dispatched by the Interpreter
        log_conversation_impl(relay_dir, "problem", "I need a login form")
        result = dispatch_behaviour_impl(relay_dir, "a user can log in")
        behaviour_id = result["behaviour_id"]

        # 2. Pipeline: analyst → examiner → builder → examiner → analyst
        router = FakeRouter([
            # Analyst: behaviour-to-implement → behaviour
            json.dumps({"actor": "user", "outcome": "can log in", "boundaries": "web"}),
            # Examiner: behaviour → expectation
            json.dumps({
                "expectations": [{"id": "E1", "statement": "login works"}],
                "integration_expectation": "form submits",
            }),
            # Examiner: evidence → evaluation (satisfied)
            json.dumps({"satisfied": ["E1"], "unmet": [], "summary": "login works"}),
            # Analyst: behaviour-status → re-authored status
            json.dumps({
                "behaviour_id": behaviour_id,
                "outcome": "solved",
                "summary": "the login behaviour is complete",
            }),
        ])

        analyst = AnalystWorker(config, router, relay_dir)
        examiner = ExaminerWorker(config, router, relay_dir)
        builder = BuilderWorker(config, router, relay_dir, harness_runner=FakeHarness())

        # drive the pipeline in order until the status reaches the interpreter
        analyst.run_once()      # behaviour → examiner inbox
        examiner.run_once()     # expectation → builder inbox
        builder.run_once()      # evidence → examiner inbox
        examiner.run_once()     # behaviour-status → analyst inbox
        analyst.run_once()      # behaviour-status → interpreter inbox

        # 3. The Interpreter polls and gets "solved"
        status = check_status_impl(relay_dir, behaviour_id)
        assert status["outcome"] == "solved"
        assert "login" in status["summary"]

        # 4. reply_to chain is intact end to end
        final = list_ledger(relay_dir, behaviour_id=behaviour_id)
        by_id = {m.id: m for m in final}
        # follow reply_to from the interpreter-bound status all the way back
        chain = []
        current = [m for m in final if m.to_role == "interpreter" and m.type == "behaviour-status"][0]
        while current.reply_to:
            chain.append(current.type)
            current = by_id[current.reply_to]
        chain.append(current.type)
        # the chain ends at the behaviour-to-implement the Interpreter dispatched
        assert current.type == "behaviour-to-implement"
        assert current.behaviour_id == behaviour_id
