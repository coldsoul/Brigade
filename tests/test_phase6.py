"""Tests for Phase 6 — Designer worker, review adapters, and interpreter tools."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.harness import HarnessResult
from brigade.interpreter import (
    check_design_status_impl,
    dispatch_design_request_impl,
)
from brigade.messages import Message, validate
from brigade.review import (
    APPROVED,
    BasicAdapter,
    LavishAdapter,
    select_review_adapter,
)
from brigade.storage import consume, deliver, list_inbox, read_message
from brigade.workers import DesignerWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _id() -> str:
    return str(ULID())


def _make_message(msg_type, from_role, to_role, payload, behaviour_id=None) -> Message:
    return Message(
        id=_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=behaviour_id or _id(),
        payload=payload,
    )


def _make_config(max_loops: int = 3, review_tool: str = "basic") -> Config:
    return Config.model_validate(
        {
            "project": {"max_loops": max_loops},
            "roles": {
                "designer": {
                    "model": "deepseek/deepseek-v4-pro",
                    "harness": "opencode",
                    "review_tool": review_tool,
                },
            },
        }
    )


@pytest.fixture
def brigade_dir(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    (project_root / "README.md").write_text("# p\n")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=project_root, check=True, capture_output=True,
    )
    d = project_root / ".brigade"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "interpreter", "designer"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    (d / "personas").mkdir()
    return d


class FakeHarness:
    """Writes the HTML artifact and a summary JSON, like the Designer harness."""

    def __init__(self):
        self.prompts: list[str] = []
        self.workdirs: list[Path] = []

    def run(self, harness, model, workdir, prompt):
        self.prompts.append(prompt)
        self.workdirs.append(workdir)
        paths = re.findall(r"to this exact path:\n(\S+)", prompt)
        assert len(paths) == 2, f"expected 2 paths in prompt, got {paths}"
        artifact_path = Path(paths[0])
        summary_path = Path(paths[1])
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("<html><body><h1>Concept</h1></body></html>")
        summary_path.write_text(json.dumps({"description": "a clean, minimal layout"}))
        return HarnessResult(command=["fake"], exit_code=0, stdout="ok", stderr="")


class FakeAdapter:
    """Returns scripted review results, recording each call."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    def review(self, artifact_path: str) -> str:
        self.calls.append(artifact_path)
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Validator / topology
# ---------------------------------------------------------------------------

class TestDesignMessages:
    def test_design_request_is_valid(self):
        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        validate(msg)  # no raise

    def test_design_result_is_valid(self):
        msg = _make_message(
            "design-result",
            "designer",
            "interpreter",
            {"artifact_ref": "concept.html", "description": "minimal", "iterations": 1},
        )
        validate(msg)  # no raise


# ---------------------------------------------------------------------------
# Review adapter selection
# ---------------------------------------------------------------------------

class TestReviewSelection:
    def test_auto_picks_lavish_when_available(self):
        assert isinstance(select_review_adapter("auto", lavish_present=True), LavishAdapter)

    def test_auto_falls_back_to_basic(self):
        assert isinstance(select_review_adapter("auto", lavish_present=False), BasicAdapter)

    def test_explicit_basic_overrides_available_lavish(self):
        assert isinstance(select_review_adapter("basic", lavish_present=True), BasicAdapter)

    def test_explicit_lavish(self):
        assert isinstance(select_review_adapter("lavish", lavish_present=False), LavishAdapter)


class TestLavishAdapter:
    def _fake_run(self, monkeypatch, poll_stdout):
        import brigade.review as review_mod

        calls: list[list[str]] = []

        class FakeResult:
            def __init__(self, stdout):
                self.stdout = stdout
                self.stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "poll" in cmd:
                return FakeResult(poll_stdout)
            return FakeResult("")

        monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
        return calls

    def test_uses_npx_lavish_axi_and_returns_feedback(self, monkeypatch):
        calls = self._fake_run(monkeypatch, poll_stdout="make it darker")
        adapter = LavishAdapter()
        result = adapter.review("concept.html")
        assert result == "make it darker"
        assert calls[0][:3] == ["npx", "-y", "lavish-axi"]
        assert calls[1][:4] == ["npx", "-y", "lavish-axi", "poll"]

    def test_empty_poll_output_means_approved(self, monkeypatch):
        self._fake_run(monkeypatch, poll_stdout="")
        adapter = LavishAdapter()
        assert adapter.review("concept.html") == APPROVED

    def test_session_ended_marker_means_approved(self, monkeypatch):
        poll_stdout = (
            "This was the last feedback before the user ended the session. "
            "Stop polling concept.html and do not reopen it."
        )
        self._fake_run(monkeypatch, poll_stdout=poll_stdout)
        adapter = LavishAdapter()
        assert adapter.review("concept.html") == APPROVED


# ---------------------------------------------------------------------------
# Interpreter tools
# ---------------------------------------------------------------------------

class TestInterpreterTools:
    def test_dispatch_design_request(self, tmp_path):
        d = tmp_path / ".brigade"
        (d / "ledger").mkdir(parents=True)
        (d / "mailboxes" / "designer" / "inbox").mkdir(parents=True)
        (d / "mailboxes" / "interpreter" / "inbox").mkdir(parents=True)

        result = dispatch_design_request_impl(d, "a landing page")
        inbox = list_inbox("designer", d)
        assert len(inbox) == 1
        msg = read_message(inbox[0], d)
        assert msg.type == "design-request"
        assert msg.to_role == "designer"
        assert msg.payload["text"] == "a landing page"
        assert msg.behaviour_id == result["behaviour_id"]

    def test_check_design_status_pending_then_done(self, tmp_path):
        d = tmp_path / ".brigade"
        (d / "ledger").mkdir(parents=True)
        (d / "mailboxes" / "designer" / "inbox").mkdir(parents=True)
        (d / "mailboxes" / "interpreter" / "inbox").mkdir(parents=True)

        bid = _id()
        assert check_design_status_impl(d, bid)["status"] == "pending"

        result = _make_message(
            "design-result",
            "designer",
            "interpreter",
            {"artifact_ref": "concept.html", "description": "minimal", "iterations": 2},
            behaviour_id=bid,
        )
        deliver(result, d)

        status = check_design_status_impl(d, bid)
        assert status["status"] == "done"
        assert status["artifact_ref"] == "concept.html"
        assert status["description"] == "minimal"


# ---------------------------------------------------------------------------
# Designer worker
# ---------------------------------------------------------------------------

class TestDesignerWorker:
    def test_approved_first_try(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter([APPROVED])
        worker = DesignerWorker(
            _make_config(), None, brigade_dir, harness_runner=harness, review_adapter=adapter
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        reply = worker.process(msg)

        assert reply.type == "design-result"
        assert reply.to_role == "interpreter"
        assert reply.payload["iterations"] == 1
        assert reply.payload["artifact_ref"].endswith("concept.html")
        assert "clean, minimal" in reply.payload["description"]

    def test_worktree_and_html_generated(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter([APPROVED])
        worker = DesignerWorker(
            _make_config(), None, brigade_dir, harness_runner=harness, review_adapter=adapter
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        worker.process(msg)

        worktree = brigade_dir.parent / ".brigade" / "work" / f"{msg.behaviour_id}-design"
        assert (worktree / "concept.html").exists()
        assert (worktree / "concept.html").read_text().startswith("<html>")

    def test_feedback_round_trip_regenerates(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter(["make the header darker", APPROVED])
        worker = DesignerWorker(
            _make_config(), None, brigade_dir, harness_runner=harness, review_adapter=adapter
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        reply = worker.process(msg)

        # two review rounds → two harness invocations, feedback fed back in
        assert len(harness.prompts) == 2
        assert reply.payload["iterations"] == 2
        assert "make the header darker" in harness.prompts[1]

    def test_iterations_cap_reached_marks_cap_note(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter(["nope", "still nope", "again"])  # never approves
        worker = DesignerWorker(
            _make_config(max_loops=3), None, brigade_dir,
            harness_runner=harness, review_adapter=adapter,
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        reply = worker.process(msg)

        assert reply.payload["iterations"] == 3
        assert "ITERATION CAP REACHED" in reply.payload["description"]

    def test_prompt_includes_persona_boundary(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter([APPROVED])
        worker = DesignerWorker(
            _make_config(), None, brigade_dir, harness_runner=harness, review_adapter=adapter
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        worker.process(msg)

        assert "Never implement" in harness.prompts[0]
        assert "backend logic" in harness.prompts[0]

    def test_deliver_and_run_once(self, brigade_dir):
        harness = FakeHarness()
        adapter = FakeAdapter([APPROVED])
        worker = DesignerWorker(
            _make_config(), None, brigade_dir, harness_runner=harness, review_adapter=adapter
        )

        msg = _make_message(
            "design-request", "interpreter", "designer", {"text": "a landing page"}
        )
        deliver(msg, brigade_dir)
        worker.run_once()

        inbox = list_inbox("interpreter", brigade_dir)
        assert len(inbox) == 1
        reply = consume("interpreter", inbox[0], brigade_dir)
        assert reply.type == "design-result"
        assert reply.reply_to == msg.id
