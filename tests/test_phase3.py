"""Tests for Phase 3 — Builder worker, worktrees, and harness invocation."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from relay.config import Config
from relay.harness import HarnessResult
from relay.messages import Message
from relay.storage import consume, deliver, list_inbox
from relay.worktree import current_branch, has_worktree
from relay.workers import BuilderWorker


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
def relay_dir(tmp_path: Path) -> Path:
    """A temp project with a real git repo and .relay/ layout."""
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

    d = project_root / ".relay"
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
    def test_expectation_creates_worktree_and_branch(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        deliver(incoming, relay_dir)

        worker.run_once()

        worktree = relay_dir.parent / ".relay" / "work" / behaviour_id
        assert has_worktree(relay_dir.parent, behaviour_id)
        assert current_branch(worktree) == f"relay/{behaviour_id}"

    def test_harness_invoked_inside_worktree(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        assert harness.workdirs[0] == relay_dir.parent / ".relay" / "work" / behaviour_id

    def test_executed_evidence_round_trip(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        deliver(incoming, relay_dir)
        worker.run_once()

        inbox = list_inbox("examiner", relay_dir)
        assert len(inbox) == 1
        reply = consume("examiner", inbox[0], relay_dir)
        assert reply.type == "evidence"
        item = reply.payload["evidence"][0]
        assert item["confidence"] == "executed"
        assert item["execution"]["command"]
        assert item["execution"]["raw_output"] == "5"

    def test_test_files_touched_are_real(self, relay_dir):
        harness = FakeHarness(
            evidence_payload=_executed_evidence(),
            files_to_create={"tests/test_adder.py": "def test_add(): assert add(2,3)==5\n"},
        )
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        deliver(incoming, relay_dir)
        worker.run_once()

        worktree = relay_dir.parent / ".relay" / "work" / behaviour_id
        assert (worktree / "tests" / "test_adder.py").exists()

    def test_prompt_includes_forbidden_leakage_rule(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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

    def test_verdict_resumes_same_worktree(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        worktree = relay_dir.parent / ".relay" / "work" / behaviour_id
        assert current_branch(worktree) == f"relay/{behaviour_id}"

        # the verdict prompt carried the unmet expectation and reason
        assert "wrong on negatives" in harness.prompts[1]

    def test_non_executable_expectation_marked_narrative(self, relay_dir):
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
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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

    def test_harness_failure_produces_narrative_fallback(self, relay_dir):
        harness = FakeHarness(evidence_payload=None)  # harness fails, writes nothing
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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

    def test_crash_survival_worktree_idempotent(self, relay_dir):
        harness = FakeHarness(evidence_payload=_executed_evidence())
        worker = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)

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
        worker2 = BuilderWorker(_make_config(), None, relay_dir, harness_runner=harness)
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
        worktree = relay_dir.parent / ".relay" / "work" / behaviour_id
        assert has_worktree(relay_dir.parent, behaviour_id)
        assert current_branch(worktree) == f"relay/{behaviour_id}"
