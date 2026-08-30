"""Tests for patch 17 — worker error surfacing in the TUI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.messages import Message
from brigade.storage import deliver
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


def _events(caplog) -> list[str | None]:
    return [getattr(r, "event", None) for r in caplog.records]


# ---------------------------------------------------------------------------
# Deliverable 1 — RoleWorker.run_once catches per-message failures
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


class TestRunOnceErrorSurfacing:
    def test_message_failure_is_logged_and_skipped(self, caplog, brigade_dir):
        ok = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "ok"})
        bad = _msg("behaviour-to-implement", "interpreter", "analyst", {"text": "bad"})
        deliver(ok, brigade_dir)
        deliver(bad, brigade_dir)

        worker = _BoomWorker(Config(), _DummyRouter(), brigade_dir, boom={bad.id})

        caplog.set_level(logging.INFO)
        worker.run_once()  # must not raise

        assert worker.processed == [ok.id]
        assert "worker_error" in _events(caplog)


class _CrashWorker(RoleWorker):
    role = "analyst"

    def run_once(self):
        raise RuntimeError("loop boom")


class TestRunLifecycleSurfacing:
    def test_loop_crash_logs_crash_and_exit(self, caplog, brigade_dir):
        worker = _CrashWorker(Config(), _DummyRouter(), brigade_dir)

        caplog.set_level(logging.INFO)
        worker.run()  # must not raise

        events = _events(caplog)
        assert "worker_start" in events
        assert "worker_crashed" in events
        assert "worker_exit" in events


# ---------------------------------------------------------------------------
# Deliverable 1b — Sentinel.run catches scan failures
# ---------------------------------------------------------------------------

class _StopLoop(Exception):
    pass


class TestSentinelErrorSurfacing:
    def test_scan_failure_logged_and_lifecycle(self, caplog, monkeypatch, brigade_dir):
        import time as time_module

        from brigade.sentinel import Sentinel

        sentinel = Sentinel(Config(), _DummyRouter(), brigade_dir, scan_every=1, poll_interval=0)
        scans = {"count": 0}

        def _scan_once():
            scans["count"] += 1
            raise RuntimeError("scan boom")

        def _sleep(_seconds):
            raise _StopLoop()

        monkeypatch.setattr(sentinel, "_new_count", lambda: 1)
        monkeypatch.setattr(sentinel, "scan_once", _scan_once)
        monkeypatch.setattr(time_module, "sleep", _sleep)

        caplog.set_level(logging.INFO)
        sentinel.run()  # must not raise

        events = _events(caplog)
        assert "worker_start" in events
        assert "worker_error" in events
        assert "worker_crashed" in events
        assert "worker_exit" in events
        assert scans["count"] == 1


# ---------------------------------------------------------------------------
# Deliverable 2 — TUI renders worker lifecycle events
# ---------------------------------------------------------------------------

class _DeadThread:
    def __init__(self, name: str):
        self.name = name

    def is_alive(self) -> bool:
        return False


def _status(app, role: str) -> str:
    from textual.widgets import DataTable

    table = app.overview.query_one("#roles", DataTable)
    return table.get_row_at(table.get_row_index(role))[1]


class TestTuiWorkerEvents:
    def test_worker_error_renders_red(self, brigade_dir):
        from brigade.tui.app import BrigadeApp
        from brigade.tui.events import RoleEvent

        async def run():
            app = BrigadeApp(brigade_dir)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.post_message(
                    RoleEvent(role="builder", event="worker_error", text="error processing message 01M")
                )
                await pilot.pause()
                assert "ERROR" in _status(app, "builder")

        asyncio.run(run())

    def test_worker_crashed_renders_red(self, brigade_dir):
        from brigade.tui.app import BrigadeApp
        from brigade.tui.events import RoleEvent

        async def run():
            app = BrigadeApp(brigade_dir)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.post_message(
                    RoleEvent(role="builder", event="worker_crashed", text="worker loop crashed")
                )
                await pilot.pause()
                assert "CRASHED" in _status(app, "builder")

        asyncio.run(run())

    def test_worker_exit_renders_stopped(self, brigade_dir):
        from brigade.tui.app import BrigadeApp
        from brigade.tui.events import RoleEvent

        async def run():
            app = BrigadeApp(brigade_dir)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.post_message(
                    RoleEvent(role="builder", event="worker_exit", text="worker exiting")
                )
                await pilot.pause()
                assert _status(app, "builder") == "stopped"

        asyncio.run(run())

    def test_crash_then_exit_keeps_crashed(self, brigade_dir):
        from brigade.tui.app import BrigadeApp
        from brigade.tui.events import RoleEvent

        async def run():
            app = BrigadeApp(brigade_dir)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.post_message(
                    RoleEvent(role="builder", event="worker_crashed", text="worker loop crashed")
                )
                await pilot.pause()
                app.post_message(
                    RoleEvent(role="builder", event="worker_exit", text="worker exiting")
                )
                await pilot.pause()
                assert "CRASHED" in _status(app, "builder")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Deliverable 3 — TUI liveness check marks killed workers as DIED
# ---------------------------------------------------------------------------

class TestTuiLiveness:
    def test_dead_thread_marks_died(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        async def run():
            app = BrigadeApp(brigade_dir, worker_threads=[_DeadThread("analyst")])
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.overview._check_liveness()
                assert "DIED" in _status(app, "analyst")

        asyncio.run(run())

    def test_exited_role_not_marked_died(self, brigade_dir):
        from brigade.tui.app import BrigadeApp
        from brigade.tui.events import RoleEvent

        async def run():
            app = BrigadeApp(brigade_dir, worker_threads=[_DeadThread("analyst")])
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.post_message(RoleEvent(role="analyst", event="worker_exit", text="worker exiting"))
                await pilot.pause()
                app.overview._check_liveness()
                status = _status(app, "analyst")
                assert status == "stopped"
                assert "DIED" not in status

        asyncio.run(run())

    def test_alive_thread_not_marked_died(self, brigade_dir):
        import threading

        from brigade.tui.app import BrigadeApp

        stop = threading.Event()
        alive = threading.Thread(target=stop.wait, name="analyst")
        alive.start()

        try:
            async def run():
                app = BrigadeApp(brigade_dir, worker_threads=[alive])
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.overview._check_liveness()
                    status = _status(app, "analyst")
                    assert "DIED" not in status

            asyncio.run(run())
        finally:
            stop.set()
            alive.join(timeout=1)


# ---------------------------------------------------------------------------
# Deliverable 4 — BrigadeApp maps worker threads by name
# ---------------------------------------------------------------------------

class TestBrigadeAppThreads:
    def test_threads_mapped_by_name(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        app = BrigadeApp(brigade_dir, worker_threads=[_DeadThread("analyst"), _DeadThread("sentinel")])
        assert set(app.worker_threads) == {"analyst", "sentinel"}
        assert app.worker_threads["analyst"].name == "analyst"

    def test_no_threads_is_ok(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        app = BrigadeApp(brigade_dir)
        assert app.worker_threads == {}
