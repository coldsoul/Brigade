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

class _FakeSupervisor:
    """Minimal supervisor stand-in for TUI liveness tests."""

    def __init__(self, statuses, restarted=None):
        self.statuses = statuses
        self.restarted = restarted or []
        self.check_calls = 0

    def check_and_restart(self) -> list[str]:
        self.check_calls += 1
        return self.restarted

    def status(self):
        return self.statuses


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
# Deliverable 3 — TUI liveness is driven by the supervisor
# ---------------------------------------------------------------------------

class TestTuiLiveness:
    def test_down_worker_shows_backoff(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        sup = _FakeSupervisor(
            {"analyst": {"alive": False, "restarts": 0, "retry_in": 2.0}}
        )

        async def run():
            app = BrigadeApp(brigade_dir, supervisor=sup)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.overview._check_liveness()
                assert "DOWN" in _status(app, "analyst")
                assert sup.check_calls == 1

        asyncio.run(run())

    def test_restarted_worker_flashes(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        sup = _FakeSupervisor(
            {"analyst": {"alive": True, "restarts": 1, "retry_in": 0.0}},
            restarted=["analyst"],
        )

        async def run():
            app = BrigadeApp(brigade_dir, supervisor=sup)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.overview._check_liveness()
                assert "RESTARTED" in _status(app, "analyst")

        asyncio.run(run())

    def test_alive_worker_untouched(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        sup = _FakeSupervisor(
            {"analyst": {"alive": True, "restarts": 0, "retry_in": 0.0}}
        )

        async def run():
            app = BrigadeApp(brigade_dir, supervisor=sup)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.overview._check_liveness()
                status = _status(app, "analyst")
                assert "DOWN" not in status
                assert "RESTARTED" not in status

        asyncio.run(run())

    def test_restart_count_badge_on_role_row(self, brigade_dir):
        from textual.widgets import DataTable

        from brigade.tui.app import BrigadeApp

        sup = _FakeSupervisor(
            {"analyst": {"alive": True, "restarts": 3, "retry_in": 0.0}}
        )

        async def run():
            app = BrigadeApp(brigade_dir, supervisor=sup)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                app.overview._check_liveness()
                table = app.overview.query_one("#roles", DataTable)
                row = table.get_row_at(table.get_row_index("analyst"))
                assert "↻3" in row[0]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Deliverable 4 — BrigadeApp holds the supervisor
# ---------------------------------------------------------------------------

class TestBrigadeAppSupervisor:
    def test_supervisor_stored(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        sup = _FakeSupervisor({})
        app = BrigadeApp(brigade_dir, supervisor=sup)
        assert app.supervisor is sup

    def test_no_supervisor_is_ok(self, brigade_dir):
        from brigade.tui.app import BrigadeApp

        app = BrigadeApp(brigade_dir)
        assert app.supervisor is None
