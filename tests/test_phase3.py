"""Tests for Phase 3 — Builder worker, worktrees, and harness invocation."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.harness import HarnessResult, HarnessRunner
from brigade.messages import Message
from brigade.storage import consume, deliver, list_inbox
from brigade.worktree import current_branch, has_worktree
from brigade.workers import BuilderWorker


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
) -> Message:
    return Message(
        id=_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=behaviour_id or _id(),
        payload=payload,
    )


def _make_config() -> Config:
    return Config.model_validate(
        {
            "project": {"max_loops": 3},
            "roles": {
                "analyst": {"model": "anthropic/claude-sonnet-5"},
                "examiner": {"model": "anthropic/claude-sonnet-5"},
                "builder": {
                    "model": "anthropic/claude-sonnet-5",
                    "harness": "claude",
                },
            },
        }
    )


@pytest.fixture
def brigade_dir(tmp_path: Path) -> Path:
    """A temp project with a real git repo and .brigade/ layout."""
    project_root = tmp_path / "project"
    project_root.mkdir()

    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    (project_root / "README.md").write_text("# project\n")
    subprocess.run(["git", "add", "README.md"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=t@t.com", "commit", "-m", "init"],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    d = project_root / ".brigade"
    (d / "ledger").mkdir(parents=True)
    for role in ("analyst", "examiner", "builder", "interpreter"):
        (d / "mailboxes" / role / "inbox").mkdir(parents=True)
    (d / "personas").mkdir()
    return d


class FakeHarness:
    """Simulates the harness: creates files and writes the evidence report."""

    def __init__(self, evidence_payload: dict | None = None, files_to_create: dict | None = None):
        self.evidence_payload = evidence_payload
        self.files_to_create = files_to_create or {}
        self.prompts: list[str] = []
        self.workdirs: list[Path] = []
        self.commands: list[list[str]] = []

    def run(self, harness, model, workdir, prompt):
        self.workdirs.append(workdir)
        self.prompts.append(prompt)
        self.commands.append(["fake", "harness"])

        for rel, content in self.files_to_create.items():
            p = workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

        if self.evidence_payload is not None:
            m = re.search(r"this exact path:\n(\S+)", prompt)
            assert m, "prompt did not contain an evidence path"
            evidence_path = Path(m.group(1))
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(self.evidence_payload))
            return HarnessResult(command=["fake"], exit_code=0, stdout="ok", stderr="")

        return HarnessResult(command=["fake"], exit_code=1, stdout="", stderr="harness failed")


def _executed_evidence() -> dict:
    return {
        "evidence": [
            {
                "expectation_id": "E1",
                "claim": "adding two numbers returns their sum",
                "execution": {
                    "command": "python -c 'from adder import add; print(add(2, 3))'",
                    "raw_output": "5",
                    "artifact_ref": None,
                },
                "confidence": "executed",
            }
        ],
        "test_files_touched": ["tests/test_adder.py"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuilder:
    def test_expectation_creates_worktree_and_branch(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add(a, b) returns the sum"}],
                "integration_expectation": "the sum is correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        deliver(incoming, brigade_dir)

        worker.run_once()

        worktree = brigade_dir.parent / ".brigade" / "work" / behaviour_id
        assert has_worktree(brigade_dir.parent, behaviour_id)
        assert current_branch(worktree) == f"brigade/{behaviour_id}"

    def test_harness_invoked_inside_worktree(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(incoming)

        assert len(harness.workdirs) == 1
        assert harness.workdirs[0] == brigade_dir.parent / ".brigade" / "work" / behaviour_id

    def test_executed_evidence_round_trip(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        deliver(incoming, brigade_dir)
        worker.run_once()

        inbox = list_inbox("examiner", brigade_dir)
        assert len(inbox) == 1
        reply = consume("examiner", inbox[0], brigade_dir)
        assert reply.type == "evidence"
        item = reply.payload["evidence"][0]
        assert item["confidence"] == "executed"
        assert item["execution"]["command"]
        assert item["execution"]["raw_output"] == "5"

    def test_test_files_touched_are_real(self, brigade_dir):
        harness = FakeHarness(
            evidence_payload=_executed_evidence(),
            files_to_create={"tests/test_adder.py": "def test_add(): assert add(2,3)==5\n"},
        )
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        deliver(incoming, brigade_dir)
        worker.run_once()

        worktree = brigade_dir.parent / ".brigade" / "work" / behaviour_id
        assert (worktree / "tests" / "test_adder.py").exists()

    def test_prompt_includes_forbidden_leakage_rule(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
        )
        worker.process(incoming)

        assert "Forbidden leakage" in harness.prompts[0]
        assert "observable outcomes only" in harness.prompts[0]

    def test_verdict_resumes_same_worktree(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        expectation = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(expectation)

        verdict = _make_message(
            "verdict",
            "examiner",
            "builder",
            {
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "wrong on negatives"}],
                "loop_count": 1,
                "escalate": False,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(verdict)

        # same worktree reused, not a fresh one
        assert len(harness.workdirs) == 2
        assert harness.workdirs[0] == harness.workdirs[1]
        worktree = brigade_dir.parent / ".brigade" / "work" / behaviour_id
        assert current_branch(worktree) == f"brigade/{behaviour_id}"

        # the verdict prompt carried the unmet expectation and reason
        assert "wrong on negatives" in harness.prompts[1]

    def test_non_executable_expectation_marked_narrative(self, brigade_dir):
        narrative = {
            "evidence": [
                {
                    "expectation_id": "E1",
                    "claim": "the behaviour requires a live third-party API",
                    "execution": {
                        "command": "",
                        "raw_output": "",
                        "artifact_ref": None,
                    },
                    "confidence": "narrative",
                }
            ],
            "test_files_touched": [],
        }
        harness = FakeHarness(evidence_payload=narrative)
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "sync with live API"}],
                "integration_expectation": "sync works",
                "loop_count": 0,
                "max_loops": 3,
            },
        )
        reply = worker.process(incoming)

        assert reply.payload["evidence"][0]["confidence"] == "narrative"

    def test_harness_failure_produces_narrative_fallback(self, brigade_dir, caplog):
        harness = FakeHarness(evidence_payload=None)  # harness fails, writes nothing
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
        )
        reply = worker.process(incoming)

        # fallback narrative evidence, never falsely "executed"
        assert reply.payload["evidence"][0]["confidence"] == "narrative"
        # the failure is surfaced, not buried in a generic claim
        assert "exit code 1" in reply.payload["evidence"][0]["claim"]
        assert "harness failed" in reply.payload["evidence"][0]["execution"]["raw_output"]
        # and it is logged as a structured event so the dashboard shows it
        failed = [r for r in caplog.records if getattr(r, "event", None) == "harness_failed"]
        assert len(failed) == 1
        assert "harness failed" in failed[0].getMessage()

    def test_opencode_harness_runs_headless_with_auto_approve(self):
        runner = HarnessRunner()
        cmd = runner._build_command("opencode", "deepseek/deepseek-flash", "hi")
        assert cmd[0] == "opencode"
        assert "--auto" in cmd
        assert "--pure" in cmd
        assert cmd[-2:] == ["--model", "deepseek/deepseek-flash"]

    def test_commit_request_commits_worktree(self, brigade_dir):
        harness = FakeHarness(
            evidence_payload=_executed_evidence(),
            files_to_create={"app.py": "def login():\n    return True\n"},
        )
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        expectation = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(expectation)

        commit_request = _make_message(
            "commit-request",
            "examiner",
            "builder",
            {"summary": "login works"},
            behaviour_id=behaviour_id,
        )
        reply = worker.process(commit_request)

        assert reply.type == "committed"
        assert reply.payload["commit_hash"] is not None
        assert reply.payload["branch"] == f"brigade/{behaviour_id}"
        assert reply.payload["summary"] == "login works"

    def test_commit_request_with_no_changes_sends_error(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())  # no files created
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        expectation = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(expectation)

        commit_request = _make_message(
            "commit-request",
            "examiner",
            "builder",
            {"summary": "login works"},
            behaviour_id=behaviour_id,
        )
        reply = worker.process(commit_request)

        assert reply.type == "committed"
        assert reply.payload["commit_hash"] is None
        assert "no changes" in reply.payload["error"]

    def test_verdict_fallback_uses_expectation_id(self, brigade_dir):
        """Regression: a failing harness on a verdict round must not KeyError.

        Verdict `unmet` items are shaped {expectation_id, reason}, not {id, ...}.
        """
        harness = FakeHarness(evidence_payload=None)  # harness fails, writes nothing
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        verdict = _make_message(
            "verdict",
            "examiner",
            "builder",
            {
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "wrong on negatives"}],
                "loop_count": 1,
                "escalate": False,
            },
            behaviour_id=_id(),
        )
        reply = worker.process(verdict)

        assert reply.type == "evidence"
        item = reply.payload["evidence"][0]
        assert item["expectation_id"] == "E1"
        assert item["confidence"] == "narrative"

    def test_crash_survival_worktree_idempotent(self, brigade_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)

        behaviour_id = _id()
        incoming = _make_message(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "add returns sum"}],
                "integration_expectation": "sum correct",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=behaviour_id,
        )
        worker.process(incoming)

        # simulate a "restart": a fresh worker resumes the same behaviour
        worker2 = BuilderWorker(_make_config(), None, brigade_dir, harness_runner=harness)
        verdict = _make_message(
            "verdict",
            "examiner",
            "builder",
            {
                "satisfied": [],
                "unmet": [{"expectation_id": "E1", "reason": "still wrong"}],
                "loop_count": 1,
                "escalate": False,
            },
            behaviour_id=behaviour_id,
        )
        worker2.process(verdict)

        # worktree survived and was reused, branch intact
        worktree = brigade_dir.parent / ".brigade" / "work" / behaviour_id
        assert has_worktree(brigade_dir.parent, behaviour_id)
        assert current_branch(worktree) == f"brigade/{behaviour_id}"
