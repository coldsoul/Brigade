"""Tests for Phase 7 — the Sentinel auditor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ulid import ULID

from brigade.config import Config
from brigade.messages import Message, ValidationError, validate
from brigade.messages.models import PAYLOAD_MODEL_BY_TYPE
from brigade.messages.topology import SENTINEL_TYPES
from brigade.sentinel import (
    Sentinel,
    check_confidence_mismatch,
    check_gamed_expectation,
    check_leakage,
    check_systemic_loop,
    sentinel_summary,
)
from brigade.storage import list_ledger, write_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_config() -> Config:
    return Config.model_validate(
        {"roles": {"sentinel": {"model": "deepseek/deepseek-v4-pro", "scan_every": 10}}}
    )


@pytest.fixture
def brigade_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".brigade"
    (d / "ledger").mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------

class TestLeakage:
    def test_catches_obvious_implementation_leak(self):
        msg = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {
                "actor": "a user",
                "outcome": "the email is checked by the validate_email function in validator.py",
                "boundaries": "",
            },
        )
        concerns = check_leakage([msg])
        assert len(concerns) == 1
        assert concerns[0]["category"] == "leakage"

    def test_clean_behaviour_has_no_false_positive(self):
        msg = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {
                "actor": "a user",
                "outcome": "an email address is accepted or rejected as valid",
                "boundaries": "web only",
            },
        )
        assert check_leakage([msg]) == []

    def test_evidence_claim_leak(self):
        msg = _msg(
            "evidence",
            "builder",
            "examiner",
            {
                "evidence": [
                    {
                        "expectation_id": "E1",
                        "claim": "added the add() helper in utils.py",
                        "execution": {"command": "pytest", "raw_output": "1 passed", "artifact_ref": None},
                        "confidence": "executed",
                    }
                ],
                "test_files_touched": ["tests/test_utils.py"],
            },
        )
        concerns = check_leakage([msg])
        assert len(concerns) == 1
        assert concerns[0]["category"] == "leakage"

    def test_design_result_description_leak(self):
        msg = _msg(
            "design-result",
            "designer",
            "interpreter",
            {
                "artifact_ref": "concept.html",
                "description": "a card layout using the React useState hook",
                "iterations": 1,
            },
        )
        concerns = check_leakage([msg])
        assert len(concerns) == 1
        assert concerns[0]["category"] == "leakage"

    def test_clean_design_result_has_no_false_positive(self):
        msg = _msg(
            "design-result",
            "designer",
            "interpreter",
            {
                "artifact_ref": "concept.html",
                "description": "a centered, minimal layout with a large verdict word",
                "iterations": 1,
            },
        )
        assert check_leakage([msg]) == []

class TestGamedExpectation:
    def test_expectation_reworded_to_match_evidence(self):
        bid = _id()
        exp1 = _msg(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "the email is validated correctly"}],
                "integration_expectation": "n/a",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=bid,
        )
        ev = _msg(
            "evidence",
            "builder",
            "examiner",
            {
                "evidence": [
                    {
                        "expectation_id": "E1",
                        "claim": "returns False for 'foo@'",
                        "execution": {"command": "pytest", "raw_output": "ok", "artifact_ref": None},
                        "confidence": "executed",
                    }
                ],
                "test_files_touched": [],
            },
            behaviour_id=bid,
        )
        exp2 = _msg(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "returns False for 'foo@'"}],
                "integration_expectation": "n/a",
                "loop_count": 1,
                "max_loops": 3,
            },
            behaviour_id=bid,
        )
        concerns = check_gamed_expectation([exp2], [exp1, ev, exp2])
        assert len(concerns) == 1
        assert concerns[0]["category"] == "gamed_expectation"

    def test_unchanged_expectation_is_not_flagged(self):
        bid = _id()
        exp1 = _msg(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "the email is validated correctly"}],
                "integration_expectation": "n/a",
                "loop_count": 0,
                "max_loops": 3,
            },
            behaviour_id=bid,
        )
        exp2 = _msg(
            "expectation",
            "examiner",
            "builder",
            {
                "expectations": [{"id": "E1", "statement": "the email is validated correctly"}],
                "integration_expectation": "n/a",
                "loop_count": 1,
                "max_loops": 3,
            },
            behaviour_id=bid,
        )
        assert check_gamed_expectation([exp2], [exp1, exp2]) == []


# ---------------------------------------------------------------------------
# Confidence mismatch
# ---------------------------------------------------------------------------

class TestConfidenceMismatch:
    def test_executed_with_empty_raw_output(self):
        ev = _msg(
            "evidence",
            "builder",
            "examiner",
            {
                "evidence": [
                    {
                        "expectation_id": "E1",
                        "claim": "the email is validated",
                        "execution": {"command": "pytest", "raw_output": "", "artifact_ref": None},
                        "confidence": "executed",
                    }
                ],
                "test_files_touched": [],
            },
        )
        concerns = check_confidence_mismatch([ev])
        assert len(concerns) == 1
        assert concerns[0]["category"] == "confidence_mismatch"

    def test_partial_confidence_is_not_flagged(self):
        ev = _msg(
            "evidence",
            "builder",
            "examiner",
            {
                "evidence": [
                    {
                        "expectation_id": "E1",
                        "claim": "some states are hard to verify",
                        "execution": {"command": "", "raw_output": "", "artifact_ref": None},
                        "confidence": "partial",
                    }
                ],
                "test_files_touched": [],
            },
        )
        assert check_confidence_mismatch([ev]) == []


# ---------------------------------------------------------------------------
# Systemic loop
# ---------------------------------------------------------------------------

class TestSystemicLoop:
    def test_single_rollup_not_per_behaviour_flags(self):
        blocked = [
            _msg(
                "behaviour-status",
                "analyst",
                "interpreter",
                {"behaviour_id": f"bid{i}", "outcome": "blocked", "summary": "cap"},
                behaviour_id=f"bid{i}",
            )
            for i in range(4)
        ]
        concerns = check_systemic_loop(blocked, blocked)
        assert len(concerns) == 1
        assert concerns[0]["category"] == "systemic_loop"

    def test_no_rollup_when_few_blocked(self):
        blocked = [
            _msg(
                "behaviour-status",
                "analyst",
                "interpreter",
                {"behaviour_id": f"bid{i}", "outcome": "blocked", "summary": "cap"},
                behaviour_id=f"bid{i}",
            )
            for i in range(2)
        ]
        assert check_systemic_loop(blocked, blocked) == []

    def test_designer_cap_systemic_rollup(self):
        capped = [
            _msg(
                "design-result",
                "designer",
                "interpreter",
                {
                    "artifact_ref": f"concept{i}.html",
                    "description": (
                        "a concept direction [ITERATION CAP REACHED — not approved. "
                        "Ask the Owner whether to proceed]"
                    ),
                    "iterations": 3,
                },
            )
            for i in range(4)
        ]
        concerns = check_systemic_loop(capped, capped)
        assert len(concerns) == 1
        assert concerns[0]["category"] == "systemic_loop"
        assert "Designer" in concerns[0]["description"]


# ---------------------------------------------------------------------------
# Sentinel scan + emit
# ---------------------------------------------------------------------------

class TestSentinel:
    def test_scan_emits_advisory(self, brigade_dir):
        leaky = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "use the validate_email function in validator.py", "boundaries": ""},
        )
        write_message(leaky, brigade_dir)

        sentinel = Sentinel(_make_config(), None, brigade_dir)
        emitted = sentinel.scan_once()

        assert len(emitted) == 1
        assert emitted[0].type == "advisory"
        assert emitted[0].from_role == "sentinel"

        ledger = list_ledger(brigade_dir)
        assert any(m.type == "advisory" for m in ledger)

    def test_clean_ledger_produces_no_advisory(self, brigade_dir):
        clean = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "a user", "outcome": "an email is accepted or rejected", "boundaries": "web"},
        )
        write_message(clean, brigade_dir)

        sentinel = Sentinel(_make_config(), None, brigade_dir)
        assert sentinel.scan_once() == []

    def test_scan_is_idempotent_via_cursor(self, brigade_dir):
        leaky = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "use validator.py", "boundaries": ""},
        )
        write_message(leaky, brigade_dir)
        sentinel = Sentinel(_make_config(), None, brigade_dir)

        assert len(sentinel.scan_once()) == 1
        # second scan sees nothing new → no duplicate advisory
        assert sentinel.scan_once() == []

    def test_summary_counts_by_category(self, brigade_dir):
        concerns = [
            {"behaviour_id": _id(), "message_id": _id(), "category": "leakage", "description": "x"},
            {"behaviour_id": _id(), "message_id": _id(), "category": "leakage", "description": "y"},
            {"behaviour_id": _id(), "message_id": _id(), "category": "confidence_mismatch", "description": "z"},
        ]
        advisory = Message(
            id=_id(),
            type="advisory",
            from_role="sentinel",
            to_role="owner",
            behaviour_id=_id(),
            payload={"concerns": concerns, "severity": "advisory"},
        )
        write_message(advisory, brigade_dir)

        summary = sentinel_summary(brigade_dir)
        assert summary == {("advisory", "leakage"): 2, ("advisory", "confidence_mismatch"): 1}


# ---------------------------------------------------------------------------
# Schema / validator
# ---------------------------------------------------------------------------

class TestSchema:
    def test_no_directive_type_anywhere(self):
        assert "directive" not in PAYLOAD_MODEL_BY_TYPE
        assert SENTINEL_TYPES == {"advisory", "warning"}

    def test_sentinel_wildcard_edge_accepted(self):
        for to_role in ("owner", "builder", "analyst", "interpreter"):
            msg = _msg(
                "advisory",
                "sentinel",
                to_role,
                {"concerns": [], "severity": "advisory"},
            )
            validate(msg)  # no raise

    def test_sentinel_directive_rejected(self):
        msg = _msg(
            "directive",
            "sentinel",
            "builder",
            {"concerns": [], "severity": "advisory"},
        )
        with pytest.raises(ValidationError):
            validate(msg)

    def test_sentinel_invalid_payload_rejected(self):
        msg = _msg(
            "advisory",
            "sentinel",
            "builder",
            {"severity": "advisory"},  # missing concerns
        )
        with pytest.raises(ValidationError):
            validate(msg)


# ---------------------------------------------------------------------------
# Model-backed leakage confirmation
# ---------------------------------------------------------------------------

class FakeRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete(self, model, prompt, role, json_mode=False, timeout=None):
        self.calls.append(prompt)
        return self.responses.pop(0)


class TestModelBackedLeakage:
    def test_model_confirms_regex_candidate(self, brigade_dir):
        leaky = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "use validator.py", "boundaries": ""},
        )
        router = FakeRouter([json.dumps({"leak": True, "reason": "mentions a file"})])
        sentinel = Sentinel(_make_config(), router, brigade_dir)
        write_message(leaky, brigade_dir)

        emitted = sentinel.scan_once()
        assert len(emitted) == 1
        assert emitted[0].payload["concerns"][0]["category"] == "leakage"

    def test_model_refutes_false_positive(self, brigade_dir):
        # "api" matches the regex pre-filter, but the model rules it's not a leak.
        candidate = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "the api is exposed to the user", "boundaries": ""},
        )
        router = FakeRouter([json.dumps({"leak": False, "reason": "not implementation detail"})])
        sentinel = Sentinel(_make_config(), router, brigade_dir)
        write_message(candidate, brigade_dir)

        assert sentinel.scan_once() == []

    def test_model_failure_falls_back_to_heuristic(self, brigade_dir):
        leaky = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "use validator.py", "boundaries": ""},
        )
        # Always-invalid model output → ModelCallError → fall back to flagging.
        router = FakeRouter(["garbage"] * 10)
        sentinel = Sentinel(_make_config(), router, brigade_dir)
        write_message(leaky, brigade_dir)

        emitted = sentinel.scan_once()
        assert len(emitted) == 1

    def test_model_only_called_for_candidates(self, brigade_dir):
        clean = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "an email is accepted or rejected", "boundaries": "web"},
        )
        leaky = _msg(
            "behaviour",
            "analyst",
            "examiner",
            {"actor": "u", "outcome": "use validator.py", "boundaries": ""},
        )
        router = FakeRouter([json.dumps({"leak": True, "reason": "file"})])
        sentinel = Sentinel(_make_config(), router, brigade_dir)
        write_message(clean, brigade_dir)
        write_message(leaky, brigade_dir)

        sentinel.scan_once()
        # only the regex candidate triggered a model call — not the clean message
        assert len(router.calls) == 1
