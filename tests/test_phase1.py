"""Tests for Phase 1 — Ledger and Mailbox Primitives."""

from __future__ import annotations

import json
import os
import signal
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ulid import ULID

from relay.messages import (
    Message,
    TOPOLOGY,
    ValidationError,
    validate,
)
from relay.storage import consume, deliver, list_inbox, list_ledger, read_message, write_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_id() -> str:
    return str(ULID())


def _make_message(
    msg_type: str,
    from_role: str,
    to_role: str,
    behaviour_id: str | None = None,
    payload: dict | None = None,
    reply_to: str | None = None,
) -> Message:
    return Message(
        id=_make_id(),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=behaviour_id or _make_id(),
        reply_to=reply_to,
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------

class TestValidator:
    """Acceptance criteria:
    - Valid message on every topology edge passes.
    - Message on nonexistent edge is rejected with structured error.
    - Legal edge but wrong type is rejected.
    - Well-formed envelope but invalid payload is rejected.
    """

    # -- valid messages for every edge --------------------------------------

    @pytest.mark.parametrize(
        "from_role, to_role, msg_type, payload",
        [
            (
                "interpreter",
                "analyst",
                "behaviour-to-implement",
                {"text": "Users need a login form"},
            ),
            (
                "analyst",
                "examiner",
                "behaviour",
                {"actor": "user", "outcome": "can log in", "boundaries": "web only"},
            ),
            (
                "examiner",
                "builder",
                "expectation",
                {
                    "expectations": [{"id": "E1", "statement": "login form exists"}],
                    "integration_expectation": "form submits",
                    "loop_count": 0,
                    "max_loops": 3,
                },
            ),
            (
                "builder",
                "examiner",
                "evidence",
                {
                    "evidence": [
                        {
                            "expectation_id": "E1",
                            "claim": "login form renders",
                            "execution": {
                                "command": "pytest tests/test_login.py",
                                "raw_output": "1 passed",
                                "artifact_ref": None,
                            },
                            "confidence": "executed",
                        }
                    ],
                    "test_files_touched": ["tests/test_login.py"],
                },
            ),
            (
                "examiner",
                "builder",
                "verdict",
                {
                    "satisfied": ["E1"],
                    "unmet": [],
                    "loop_count": 1,
                    "escalate": False,
                },
            ),
            (
                "examiner",
                "analyst",
                "behaviour-status",
                {
                    "behaviour_id": "B1",
                    "outcome": "solved",
                    "summary": "login behaviour implemented",
                },
            ),
            (
                "analyst",
                "interpreter",
                "behaviour-status",
                {
                    "behaviour_id": "B1",
                    "outcome": "solved",
                    "summary": "login is done",
                },
            ),
        ],
    )
    def test_valid_message_passes(self, from_role, to_role, msg_type, payload):
        msg = _make_message(msg_type, from_role, to_role, payload=payload)
        validate(msg)  # should not raise

    # -- invalid edge -------------------------------------------------------

    def test_rejects_nonexistent_edge(self):
        msg = _make_message(
            "behaviour-to-implement",
            "builder",
            "interpreter",
            payload={"text": "hello"},
        )
        with pytest.raises(ValidationError) as exc:
            validate(msg)
        assert "No edge builder" in exc.value.message
        assert exc.value.details["from_role"] == "builder"
        assert exc.value.details["to_role"] == "interpreter"

    # -- valid edge, wrong type ---------------------------------------------

    def test_rejects_wrong_type_on_valid_edge(self):
        msg = _make_message(
            "evidence",  # evidence is builder->examiner, not interpreter->analyst
            "interpreter",
            "analyst",
            payload={"evidence": [], "test_files_touched": []},
        )
        with pytest.raises(ValidationError) as exc:
            validate(msg)
        assert "not allowed on edge" in exc.value.message
        assert exc.value.details["type"] == "evidence"

    # -- invalid payload ----------------------------------------------------

    def test_rejects_invalid_payload_schema(self):
        msg = _make_message(
            "expectation",
            "examiner",
            "builder",
            payload={
                "expectations": [{"id": "E1"}],  # missing 'statement'
                "integration_expectation": "x",
                "loop_count": 0,
                "max_loops": 3,
            },
        )
        with pytest.raises(ValidationError) as exc:
            validate(msg)
        assert "Invalid payload" in exc.value.message
        assert len(exc.value.details["errors"]) > 0

    # -- Owner ↔ Interpreter types ------------------------------------------

    @pytest.mark.parametrize("owner_type", [
        "problem", "clarification", "roadmap", "roadmap-verdict",
        "increment", "continue-query", "feedback", "result", "question",
    ])
    def test_owner_interpreter_types_are_valid(self, owner_type):
        msg = _make_message(
            owner_type, "owner", "interpreter", payload={"text": "hello"}
        )
        validate(msg)  # should not raise

        msg_rev = _make_message(
            owner_type, "interpreter", "owner", payload={"text": "hello"}
        )
        validate(msg_rev)  # should not raise


# ---------------------------------------------------------------------------
# Ledger tests
# ---------------------------------------------------------------------------

class TestLedger:
    """Acceptance criteria:
    - write + read round-trip.
    - Atomic write (no partial files on crash).
    - list_ledger with and without behaviour_id filter.
    - Messages sorted chronologically (ULID order).
    """

    @pytest.fixture
    def relay_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / ".relay"
        d.mkdir()
        (d / "ledger").mkdir()
        return d

    def test_write_and_read_round_trip(self, relay_dir):
        msg = _make_message(
            "behaviour-to-implement",
            "interpreter",
            "analyst",
            payload={"text": "need a login"},
        )
        path = write_message(msg, relay_dir)
        assert path.exists()

        restored = read_message(msg.id, relay_dir)
        assert restored.id == msg.id
        assert restored.type == msg.type
        assert restored.from_role == msg.from_role
        assert restored.to_role == msg.to_role
        assert restored.behaviour_id == msg.behaviour_id
        assert restored.payload == msg.payload

    def test_atomic_write_no_partial_files(self, relay_dir):
        """Simulate a crash during write — no corrupt ledger files remain."""
        ledger_dir = relay_dir / "ledger"

        # Monkey-patch rename to simulate crash
        original_rename = os.rename

        def crashing_rename(src, dst):
            # Simulate a crash before rename completes
            raise OSError("simulated crash")

        os.rename = crashing_rename
        try:
            msg = _make_message(
                "behaviour-to-implement",
                "interpreter",
                "analyst",
                payload={"text": "crash test"},
            )
            with pytest.raises(OSError):
                write_message(msg, relay_dir)
        finally:
            os.rename = original_rename

        # The temp file should have been left behind, but no .json ledger file
        json_files = list(ledger_dir.glob("*.json"))
        assert len(json_files) == 0, f"partial .json files found: {json_files}"

        # Clean up any leftover temp files
        for tmp in ledger_dir.glob(".*.tmp"):
            tmp.unlink()

    def test_list_ledger_all(self, relay_dir):
        msg1 = _make_message("behaviour-to-implement", "interpreter", "analyst",
                             payload={"text": "first"})
        msg2 = _make_message("behaviour", "analyst", "examiner",
                             payload={"actor": "u", "outcome": "o", "boundaries": "b"})
        write_message(msg1, relay_dir)
        write_message(msg2, relay_dir)

        all_msgs = list_ledger(relay_dir)
        assert len(all_msgs) == 2
        # ULIDs should sort chronologically
        assert all_msgs[0].id == msg1.id
        assert all_msgs[1].id == msg2.id

    def test_list_ledger_filter_by_behaviour_id(self, relay_dir):
        bid = _make_id()
        msg_a = _make_message("behaviour-to-implement", "interpreter", "analyst",
                              behaviour_id=bid, payload={"text": "a"})
        msg_b = _make_message("behaviour", "analyst", "examiner",
                              behaviour_id=bid, payload={"actor": "u", "outcome": "o", "boundaries": "b"})
        msg_other = _make_message("behaviour-to-implement", "interpreter", "analyst",
                                  payload={"text": "other"})

        write_message(msg_a, relay_dir)
        write_message(msg_b, relay_dir)
        write_message(msg_other, relay_dir)

        filtered = list_ledger(relay_dir, behaviour_id=bid)
        assert len(filtered) == 2
        assert {m.id for m in filtered} == {msg_a.id, msg_b.id}

    def test_list_ledger_empty_dir(self, relay_dir):
        assert list_ledger(relay_dir) == []

    def test_read_nonexistent_message(self, relay_dir):
        with pytest.raises(FileNotFoundError):
            read_message("01NONEXISTENT", relay_dir)


# ---------------------------------------------------------------------------
# Mailbox tests
# ---------------------------------------------------------------------------

class TestMailbox:
    """Acceptance criteria:
    - deliver + list_inbox + consume round-trip.
    - Consume removes pointer, ledger entry persists.
    """

    @pytest.fixture
    def relay_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / ".relay"
        d.mkdir()
        (d / "ledger").mkdir()
        for role in ("analyst", "examiner", "builder", "interpreter"):
            (d / "mailboxes" / role / "inbox").mkdir(parents=True)
        return d

    def test_deliver_list_consume_round_trip(self, relay_dir):
        msg = _make_message(
            "behaviour-to-implement",
            "interpreter",
            "analyst",
            payload={"text": "need a login form"},
        )

        # Deliver puts it in analyst's inbox
        deliver(msg, relay_dir)

        # List shows it
        inbox = list_inbox("analyst", relay_dir)
        assert inbox == [msg.id]

        # Ledger file exists
        assert (relay_dir / "ledger" / f"{msg.id}.json").exists()

        # Consume returns the message
        consumed = consume("analyst", msg.id, relay_dir)
        assert consumed.id == msg.id
        assert consumed.payload == msg.payload

        # Inbox pointer is gone
        assert list_inbox("analyst", relay_dir) == []

        # Ledger entry still exists
        assert (relay_dir / "ledger" / f"{msg.id}.json").exists()

    def test_list_inbox_empty(self, relay_dir):
        assert list_inbox("analyst", relay_dir) == []

    def test_list_inbox_nonexistent_role(self, relay_dir):
        assert list_inbox("nonexistent", relay_dir) == []

    def test_consume_missing_pointer(self, relay_dir):
        with pytest.raises(FileNotFoundError):
            consume("analyst", "01NONEXISTENT", relay_dir)

    def test_multiple_messages_in_inbox_oldest_first(self, relay_dir):
        msg1 = _make_message("behaviour-to-implement", "interpreter", "analyst",
                             payload={"text": "first"})
        msg2 = _make_message("behaviour-to-implement", "interpreter", "analyst",
                             payload={"text": "second"})
        deliver(msg1, relay_dir)
        deliver(msg2, relay_dir)

        inbox = list_inbox("analyst", relay_dir)
        assert len(inbox) == 2
        # ULIDs sort chronologically, so msg1 (older) comes first
        assert inbox == [msg1.id, msg2.id]


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """Acceptance criteria:
    - Two behaviour-status messages for the same behaviour linked via reply_to
      are written as two distinct ledger entries and both are retrievable.
    """

    @pytest.fixture
    def relay_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / ".relay"
        d.mkdir()
        (d / "ledger").mkdir()
        for role in ("analyst", "examiner", "builder", "interpreter"):
            (d / "mailboxes" / role / "inbox").mkdir(parents=True)
        return d

    def test_linked_behaviour_status_messages(self, relay_dir):
        behaviour_id = _make_id()

        # Examiner → Analyst behaviour-status
        exam_status = _make_message(
            "behaviour-status",
            "examiner",
            "analyst",
            behaviour_id=behaviour_id,
            payload={
                "behaviour_id": behaviour_id,
                "outcome": "solved",
                "summary": "expectations all met",
            },
        )
        deliver(exam_status, relay_dir)

        # Analyst → Interpreter behaviour-status, linked via reply_to
        analyst_status = _make_message(
            "behaviour-status",
            "analyst",
            "interpreter",
            behaviour_id=behaviour_id,
            reply_to=exam_status.id,
            payload={
                "behaviour_id": behaviour_id,
                "outcome": "solved",
                "summary": "login behaviour is done",
            },
        )
        deliver(analyst_status, relay_dir)

        # Both in ledger
        ledger = list_ledger(relay_dir, behaviour_id=behaviour_id)
        assert len(ledger) == 2

        # Second message links back to the first
        restored = read_message(analyst_status.id, relay_dir)
        assert restored.reply_to == exam_status.id

        # Both are distinct files
        assert exam_status.id != analyst_status.id

    def test_valid_message_does_not_write_on_validation_failure(self, relay_dir):
        """A message that fails validation must not touch disk at all."""
        msg = _make_message(
            "evidence",  # wrong edge: interpreter->analyst doesn't allow evidence
            "interpreter",
            "analyst",
            payload={"evidence": [], "test_files_touched": []},
        )
        with pytest.raises(ValidationError):
            write_message(msg, relay_dir)

        # Nothing written
        assert list_ledger(relay_dir) == []
