"""Tests for patch 20 — worker supervision and auto-restart."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.messages import Message
from brigade.storage import consume, deliver, list_inbox
from brigade.supervisor import WorkerSupervisor
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


class _DummyRouter:
    """Minimal stand-in; workers under test never call the model."""


class _TestWorker:
    """A worker object the supervisor can own and (re)spawn."""

    def __init__(self, role: str, block: bool = False):
        self.role = role
        self.block = block
        self.stop_event = threading.Event()
        self.runs = 0

    def run(self) -> None:
        self.runs += 1
        if self.block:
            self.stop_event.wait()


# ---------------------------------------------------------------------------
# Deliverable 1 — the supervisor restarts a dead worker
# ---------------------------------------------------------------------------

class TestRestart:
    def test_dead_worker_is_restarted(self):
        worker = _TestWorker("analyst")  # run() returns → thread dies
        sup = WorkerSupervisor([worker], restart_backoff=0.0, max_backoff=0.0)
        sup.start()
        sup._threads["analyst"].join(timeout=1)

        assert sup.status()["analyst"]["alive"] is False

        restarted = sup.check_and_restart()

        assert restarted == ["analyst"]
        assert sup.status()["analyst"]["restarts"] == 1
        sup.stop()

    def test_healthy_worker_never_restarted(self):
        worker = _TestWorker("analyst", block=True)
        sup = WorkerSupervisor([worker])
        sup.start()

        try:
            # wait for the thread to actually start running
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and worker.runs == 0:
                time.sleep(0.01)

            assert sup.status()["analyst"]["alive"] is True
            assert sup.check_and_restart() == []
            assert sup.status()["analyst"]["restarts"] == 0
        finally:
            sup.stop()
            worker.stop_event.set()
            sup._threads["analyst"].join(timeout=1)


# ---------------------------------------------------------------------------
# Deliverable 2 — backoff widens and is capped
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_backoff_widens(self, monkeypatch):
        import brigade.supervisor as sup_mod

        clock = {"now": 100.0}
        monkeypatch.setattr(sup_mod.time, "monotonic", lambda: clock["now"])

        sup = WorkerSupervisor([_TestWorker("analyst")], restart_backoff=2.0, max_backoff=60.0)
        sup.start()
        sup._threads["analyst"].join(timeout=1)

        sup.check_and_restart()  # restart #1
        assert sup._next_allowed["analyst"] == 102.0

        # new thread dies again; still inside the backoff window → no restart
        sup._threads["analyst"].join(timeout=1)
        assert sup.check_and_restart() == []

        clock["now"] = 103.0
        sup.check_and_restart()  # restart #2
        assert sup._next_allowed["analyst"] == 107.0  # 4s, widened
        sup.stop()

    def test_backoff_capped_at_max(self, monkeypatch):
        import brigade.supervisor as sup_mod

        monkeypatch.setattr(sup_mod.time, "monotonic", lambda: 1000.0)

        sup = WorkerSupervisor([_TestWorker("analyst")], restart_backoff=2.0, max_backoff=60.0)
        sup.start()
        sup._threads["analyst"].join(timeout=1)

        sup._restarts["analyst"] = 100
        sup.check_and_restart()

        assert sup._next_allowed["analyst"] == 1060.0  # capped at max_backoff
        sup.stop()


# ---------------------------------------------------------------------------
# Deliverable 3 — restarts are logged and stoppable
# ---------------------------------------------------------------------------

class TestRestartLogging:
    def test_restart_logs_worker_restart_event(self, caplog):
        sup = WorkerSupervisor([_TestWorker("analyst")], restart_backoff=0.0, max_backoff=0.0)
        sup.start()
        sup._threads["analyst"].join(timeout=1)

        caplog.set_level(logging.INFO)
        sup.check_and_restart()

        events = [
            r for r in caplog.records if getattr(r, "event", None) == "worker_restart"
        ]
        assert len(events) == 1
        assert getattr(events[0], "restart_count", None) == 1
        sup.stop()

    def test_stop_prevents_respawn(self):
        sup = WorkerSupervisor([_TestWorker("analyst")], restart_backoff=0.0, max_backoff=0.0)
        sup.start()
        sup._threads["analyst"].join(timeout=1)

        sup.stop()

        assert sup.check_and_restart() == []
        assert sup.status()["analyst"]["restarts"] == 0


# ---------------------------------------------------------------------------
# Deliverable 4 — end-to-end recovery (with patch 18)
# ---------------------------------------------------------------------------

class _FlakyWorker(RoleWorker):
    """Consumes a message then dies mid-processing on the first attempt.

    On a subsequent run (after the supervisor restarts it), the stranded
    message is recovered and processed normally.
    """

    role = "analyst"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attempts = 0
        self.processed: list[str] = []

    def process(self, msg):
        self.processed.append(msg.id)
        return None

    def run_once(self):
        self.attempts += 1
        if self.attempts == 1:
            ids = list_inbox(self.role, self.brigade_dir)
            if ids:
                consume(self.role, ids[0], self.brigade_dir)
            raise RuntimeError("die mid-processing")
        return super().run_once()


class TestEndToEndRecovery:
    def test_worker_killed_mid_processing_resumes_on_restart(self, brigade_dir):
        msg = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "x"})
        deliver(msg, brigade_dir)

        worker = _FlakyWorker(Config(), _DummyRouter(), brigade_dir, poll_interval=0.01)
        sup = WorkerSupervisor([worker], restart_backoff=0.0, max_backoff=0.0)
        sup.start()

        try:
            # first thread consumes then dies
            sup._threads["analyst"].join(timeout=2)
            assert worker.attempts == 1
            assert not list_inbox("analyst", brigade_dir)  # consumed

            sup.check_and_restart()

            # second thread recovers and processes the stranded message
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and msg.id not in worker.processed:
                time.sleep(0.01)

            assert msg.id in worker.processed
            assert sup.status()["analyst"]["restarts"] == 1
        finally:
            sup.stop()
            sup._threads["analyst"].join(timeout=1)
