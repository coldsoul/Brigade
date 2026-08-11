"""Message envelope and per-type payload models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Payload models — one per message type
# ---------------------------------------------------------------------------

class BehaviourToImplementPayload(BaseModel):
    """Payload for `behaviour-to-implement` (Interpreter → Analyst)."""

    text: str


class BehaviourPayload(BaseModel):
    """Payload for `behaviour` (Analyst → Examiner)."""

    actor: str
    outcome: str
    boundaries: str


class ExpectationItem(BaseModel):
    """A single checkable expectation."""

    id: str
    statement: str


class ExpectationPayload(BaseModel):
    """Payload for `expectation` (Examiner → Builder)."""

    expectations: list[ExpectationItem]
    integration_expectation: str
    loop_count: int
    max_loops: int


class EvidenceExecution(BaseModel):
    """What was actually run to produce the evidence."""

    command: str
    raw_output: str
    artifact_ref: str | None = None


class EvidenceItem(BaseModel):
    """A single piece of evidence for one expectation."""

    expectation_id: str
    claim: str
    execution: EvidenceExecution
    confidence: Literal["executed", "partial", "narrative"]


class EvidencePayload(BaseModel):
    """Payload for `evidence` (Builder → Examiner)."""

    evidence: list[EvidenceItem]
    test_files_touched: list[str]


class UnmetExpectation(BaseModel):
    """An expectation that was not satisfied."""

    expectation_id: str
    reason: str


class VerdictPayload(BaseModel):
    """Payload for `verdict` (Examiner → Builder)."""

    satisfied: list[str]
    unmet: list[UnmetExpectation]
    loop_count: int
    escalate: bool


class BehaviourStatusPayload(BaseModel):
    """Payload for `behaviour-status` (Examiner → Analyst, Analyst → Interpreter)."""

    behaviour_id: str
    outcome: Literal["solved", "blocked", "partial"]
    summary: str


class OwnerInterpreterPayload(BaseModel):
    """Shared payload for all Owner ↔ Interpreter types.

    These are logged to the ledger but never routed through mailboxes.
    """

    text: str


# ---------------------------------------------------------------------------
# Message envelope
# ---------------------------------------------------------------------------

class Message(BaseModel):
    """Full message envelope — one per ledger entry."""

    id: str  # ULID, sortable, doubles as ledger filename
    type: str
    from_role: str
    to_role: str
    behaviour_id: str
    reply_to: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = 1
    payload: dict


# ---------------------------------------------------------------------------
# Map type → payload model (used by the validator)
# ---------------------------------------------------------------------------

PAYLOAD_MODEL_BY_TYPE: dict[str, type[BaseModel]] = {
    # Machine-to-machine types
    "behaviour-to-implement": BehaviourToImplementPayload,
    "behaviour": BehaviourPayload,
    "expectation": ExpectationPayload,
    "evidence": EvidencePayload,
    "verdict": VerdictPayload,
    "behaviour-status": BehaviourStatusPayload,
    # Owner ↔ Interpreter types
    "problem": OwnerInterpreterPayload,
    "clarification": OwnerInterpreterPayload,
    "roadmap": OwnerInterpreterPayload,
    "roadmap-verdict": OwnerInterpreterPayload,
    "increment": OwnerInterpreterPayload,
    "continue-query": OwnerInterpreterPayload,
    "feedback": OwnerInterpreterPayload,
    "result": OwnerInterpreterPayload,
    "question": OwnerInterpreterPayload,
}
