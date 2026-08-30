"""Tests for patch 18 — crash-safe message processing."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.messages import Message
from brigade.storage import (
    complete,
    consume,
    deliver,
    list_inbox,
    recover_in_progress,
)
from brigade.workers.base import RoleWorker


def _id() -> str:
    return str(ULID())


def _msg(msg_type, from_role, to_role, payload) -> Message:
    return Message(
        id=_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=_id(),
        payload=payload,
    )


@pytest.fixture
def brigade_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".brigade"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "designer", "interpreter"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    return d


def _in_progress(role: str, msg_id: str, brigade_dir: Path) -> Path:
    return brigade_dir / "mailboxes" / role / "in-progress" / msg_id


class _DummyRouter:
    """Minimal stand-in; workers under test never call the model."""


# ---------------------------------------------------------------------------
# Deliverable 1 — consume() moves rather than deletes
# ---------------------------------------------------------------------------

class TestConsumeMove:
    def test_consume_moves_pointer_into_in_progress(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)

        consumed = consume("analyst", msg.id, brigade_dir)

        assert consumed.id == msg.id
        # gone from inbox, present in in-progress
        assert list_inbox("analyst", brigade_dir) == []
        assert _in_progress("analyst", msg.id, brigade_dir).is_file()


# ---------------------------------------------------------------------------
# Deliverable 2 — complete() clears the in-progress pointer
# ---------------------------------------------------------------------------

class TestComplete:
    def test_complete_removes_pointer(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)
        consume("analyst", msg.id, brigade_dir)

        complete("analyst", msg.id, brigade_dir)

        assert not _in_progress("analyst", msg.id, brigade_dir).exists()

    def test_complete_is_idempotent(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)
        consume("analyst", msg.id, brigade_dir)

        complete("analyst", msg.id, brigade_dir)
        complete("analyst", msg.id, brigade_dir)  # second call must not raise

    def test_complete_on_missing_pointer_no_raise(self, brigade_dir):
        complete("analyst", "01NONEXISTENT", brigade_dir)  # no raise


# ---------------------------------------------------------------------------
# Deliverable 3 — recovery
# ---------------------------------------------------------------------------

class TestRecoverInProgress:
    def test_crash_leaves_pointer_recoverable(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)
        consume("analyst", msg.id, brigade_dir)
        # simulated crash: no complete() before death

        assert _in_progress("analyst", msg.id, brigade_dir).is_file()

        recovered = recover_in_progress("analyst", brigade_dir)

        assert recovered == [msg.id]
        assert list_inbox("analyst", brigade_dir) == [msg.id]
        assert not _in_progress("analyst", msg.id, brigade_dir).exists()

        # and the message is processable again
        assert consume("analyst", msg.id, brigade_dir).id == msg.id

    def test_successful_process_leaves_nothing(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)
        consume("analyst", msg.id, brigade_dir)
        complete("analyst", msg.id, brigade_dir)

        assert not _in_progress("analyst", msg.id, brigade_dir).exists()
        assert recover_in_progress("analyst", brigade_dir) == []

    def test_no_in_progress_dir_returns_empty(self, brigade_dir):
        assert recover_in_progress("analyst", brigade_dir) == []


# ---------------------------------------------------------------------------
# Deliverable 4 — worker wiring
# ---------------------------------------------------------------------------

class _BoomWorker(RoleWorker):
    role = "analyst"

    def __init__(self, *args, boom: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.boom = boom or set()
        self.processed: list[str] = []

    def process(self, msg):
        if msg.id in self.boom:
            raise RuntimeError("boom")
        self.processed.append(msg.id)
        return None


class TestWorkerCrashSafety:
    def test_per_message_error_leaves_bad_message_for_recovery(self, brigade_dir):
        ok = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "ok"})
        bad = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "bad"})
        deliver(ok, brigade_dir)
        deliver(bad, brigade_dir)

        worker = _BoomWorker(Config(), _DummyRouter(), brigade_dir, boom={bad.id})
        worker.run_once()

        # good message completed, bad message survived in in-progress
        assert worker.processed == [ok.id]
        assert not _in_progress("analyst", ok.id, brigade_dir).exists()
        assert _in_progress("analyst", bad.id, brigade_dir).is_file()


class _CrashWorker(RoleWorker):
    role = "analyst"

    def run_once(self):
        raise RuntimeError("loop boom")


class TestRunRecovery:
    def test_run_recovers_and_logs_stranded_message(self, caplog, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)
        consume("analyst", msg.id, brigade_dir)  # stranded — no complete()

        worker = _CrashWorker(Config(), _DummyRouter(), brigade_dir)

        caplog.set_level(logging.INFO)
        worker.run()  # recovers, logs, then its loop crashes and returns

        recovered_events = [
            r for r in caplog.records if getattr(r, "event", None) == "message_recovered"
        ]
        assert len(recovered_events) == 1
        assert getattr(recovered_events[0], "message_id", None) == msg.id
        assert list_inbox("analyst", brigade_dir) == [msg.id]
