"""Tests for the TUI dashboard phase."""

from __future__ import annotations

import logging
import json
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.messages import Message
from brigade.storage import list_ledger, write_message
from brigade.tui.bridge import TUILogHandler
from brigade.tui.events import RoleEvent, UsageEvent
from brigade.tui.summary import summarize_behaviours
from brigade.workers.base import RoleWorker


def _id() -> str:
    return str(ULID())


def _msg(msg_type, from_role, to_role, payload, behaviour_id=None) -> Message:
    return Message(
        id=_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=behaviour_id or _id(),
        payload=payload,
    )


@pytest.fixture
def brigade_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".brigade"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "designer", "interpreter"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    return d


class _FakeApp:
    def __init__(self):
        self.messages = []

    def post_message(self, message):
        self.messages.append(message)

    def call_from_thread(self, callback, *args, **kwargs):
        callback(*args, **kwargs)


# ---------------------------------------------------------------------------
# Deliverable 0 — llm.py usage capture
# ---------------------------------------------------------------------------

class TestLLMUsage:
    def test_logs_usage_event(self, monkeypatch, caplog):
        import litellm
        from brigade.llm import LiteLLMRouter

        class _Usage:
            prompt_tokens = 123
            completion_tokens = 45

        class _Msg:
            content = "hi"

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]
            usage = _Usage()

        monkeypatch.setattr(litellm, "completion", lambda **kw: _Response())

        router = LiteLLMRouter()
        with caplog.at_level(logging.INFO):
            result = router.complete("deepseek/deepseek-v4-pro", "prompt")

        assert result == "hi"
        records = [r for r in caplog.records if getattr(r, "event", None) == "usage"]
        assert len(records) == 1
        r = records[0]
        assert r.name == "deepseek"  # provider
        assert r.model == "deepseek/deepseek-v4-pro"
        assert r.prompt_tokens == 123
        assert r.completion_tokens == 45


# ---------------------------------------------------------------------------
# Deliverable 1 — widened structured log fields
# ---------------------------------------------------------------------------

class _NoopWorker(RoleWorker):
    role = "test"

    def __init__(self, brigade_dir):
        super().__init__(Config.model_validate({"roles": {}}), None, brigade_dir)
        self.reply = None

    def process(self, msg):
        return self.reply


class TestWidenedFields:
    def test_consuming_extra_fields(self, brigade_dir, caplog):
        worker = _NoopWorker(brigade_dir)
        msg = _msg(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "x"}],
                "integration_expectation": "y",
                "loop_count": 1,
                "max_loops": 3,
            },
        )
        with caplog.at_level(logging.INFO):
            worker._dispatch(msg)

        records = [r for r in caplog.records if getattr(r, "event", None) == "consuming"]
        assert len(records) == 1
        r = records[0]
        assert r.message_type == "expectation"
        assert r.message_id == msg.id
        assert r.loop_count == 1
        assert r.max_loops == 3
        assert r.behaviour_id == msg.behaviour_id

    def test_produced_extra_fields(self, brigade_dir, caplog):
        worker = _NoopWorker(brigade_dir)
        reply = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "o", "boundaries": ""},
        )
        worker.reply = reply
        incoming = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})

        with caplog.at_level(logging.INFO):
            worker._dispatch(incoming)

        records = [r for r in caplog.records if getattr(r, "event", None) == "produced"]
        assert len(records) == 1
        r = records[0]
        assert r.message_type == "behaviour"
        assert r.message_id == reply.id
        assert r.to_role == "examiner"


# ---------------------------------------------------------------------------
# Deliverable 2 — TUILogHandler conversion
# ---------------------------------------------------------------------------

class TestTUILogHandler:
    def _record(self, **extra):
        record = logging.LogRecord(
            name=extra.pop("name", "builder"),
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=extra.pop("msg", "x"),
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_usage_record_becomes_usage_event(self):
        app = _FakeApp()
        handler = TUILogHandler(app)
        handler.emit(
            self._record(
                name="deepseek",
                event="usage",
                model="deepseek/deepseek-v4-pro",
                prompt_tokens=10,
                completion_tokens=20,
                created=123.0,
            )
        )
        assert len(app.messages) == 1
        event = app.messages[0]
        assert isinstance(event, UsageEvent)
        assert event.provider == "deepseek"
        assert event.prompt_tokens == 10
        assert event.completion_tokens == 20

    def test_role_record_becomes_role_event(self):
        app = _FakeApp()
        handler = TUILogHandler(app)
        handler.emit(
            self._record(
                name="builder",
                event="harness_end",
                behaviour_id="01M0ABC",
                duration_s=43.2,
                created=123.0,
            )
        )
        assert len(app.messages) == 1
        event = app.messages[0]
        assert isinstance(event, RoleEvent)
        assert event.role == "builder"
        assert event.event == "harness_end"
        assert event.duration_s == 43.2
        assert event.behaviour_id == "01M0ABC"

    def test_non_event_record_is_skipped(self):
        app = _FakeApp()
        handler = TUILogHandler(app)
        handler.emit(self._record(name="builder", msg="no event field"))
        assert app.messages == []


# ---------------------------------------------------------------------------
# summarize_behaviours helper
# ---------------------------------------------------------------------------

class TestSummarizeBehaviours:
    def test_groups_and_derives_status(self, brigade_dir):
        bid = _id()
        write_message(_msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"}, behaviour_id=bid), brigade_dir)
        write_message(_msg("behaviour", "analyst", "examiner", {"actor": "u", "outcome": "o", "boundaries": ""}, behaviour_id=bid), brigade_dir)
        write_message(_msg("behaviour-status", "analyst", "interpreter", {"behaviour_id": bid, "outcome": "solved", "summary": "done"}, behaviour_id=bid), brigade_dir)

        summaries = summarize_behaviours(brigade_dir)
        assert len(summaries) == 1
        s = summaries[0]
        assert s["behaviour_id"] == bid
        assert s["status"] == "solved"
        assert s["stage"] == "behaviour-status"

    def test_in_progress_when_no_terminal_status(self, brigade_dir):
        bid = _id()
        write_message(_msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"}, behaviour_id=bid), brigade_dir)

        summaries = summarize_behaviours(brigade_dir)
        assert len(summaries) == 1
        assert summaries[0]["status"] == "in-progress"

    def test_empty_ledger(self, brigade_dir):
        assert summarize_behaviours(brigade_dir) == []
