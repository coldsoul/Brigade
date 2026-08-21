"""Sentinel — a periodic, read-only auditor of the relay ledger.

Unlike the other roles, the Sentinel does not consume from an inbox in the
linear chain.  It scans `.relay/ledger/` in batches and surfaces contract
violations as `advisory` (or `warning`) messages.  For this phase it only
advises — it never writes into another role's inbox and never sends directives.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel
from ulid import ULID

from relay.messages import Message
from relay.model_calls import ModelCallError, call_for_schema
from relay.storage import list_ledger, write_message

POLL_INTERVAL = 1.0
DEFAULT_SCAN_EVERY = 10

# Gamed-expectation: how similar a reworded expectation must be to a prior
# evidence claim to count as "the bar moved to match the evidence".
SIMILARITY_THRESHOLD = 0.7

# Systemic-loop: roll up when this fraction (or more) of behaviours ended blocked.
BLOCKED_RATIO_THRESHOLD = 0.5
MIN_BEHAVIOURS_FOR_SYSTEMIC = 3

# Confidence-mismatch: an "executed" evidence with raw_output shorter than this
# is treated as trivially/emptily evidenced.
MIN_RAW_OUTPUT_LEN = 5


# ---------------------------------------------------------------------------
# Leakage heuristics
# ---------------------------------------------------------------------------

_LEAKAGE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\.[a-zA-Z]{1,4}\b"), "file extension"),
    (
        re.compile(
            r"\b(def|function|class|import|return|const|let|var|async|await|lambda)\b"
        ),
        "code keyword",
    ),
    (re.compile(r"=>"), "arrow function"),
    (
        re.compile(
            r"\b(useState|useEffect|useCallback|useMemo|props|JSX|React|Vue|"
            r"Angular|Django|Flask|FastAPI|Express|Node|argparse|pytest|api|endpoint)\b"
        ),
        "framework/library keyword",
    ),
    (re.compile(r"\b[a-z]+_[a-z0-9]+\b"), "snake_case identifier"),
]


def _has_leak(text: str) -> bool:
    return any(pattern.search(text) for pattern, _ in _LEAKAGE_PATTERNS)


# ---------------------------------------------------------------------------
# Model-backed leakage confirmation
# ---------------------------------------------------------------------------

class LeakVerdict(BaseModel):
    """What the Sentinel's model returns when confirming a leak candidate."""

    leak: bool
    reason: str


LEAK_PROMPT = """\
You are auditing a message in a multi-agent software-development pipeline for a
contract violation.

The text below is supposed to describe an observable OUTCOME in plain language,
with no implementation detail. "Implementation detail" means function names,
file names, library or framework names, programming-language keywords,
data-structure names, or code structure.

Does this text leak implementation detail that should not survive at this level
of abstraction? Answer honestly and adversarially.

TEXT:
{text}

Respond with a single JSON object: {{"leak": true/false, "reason": "..."}}
"""


# ---------------------------------------------------------------------------
# Checks — each returns a list of concern dicts
# ---------------------------------------------------------------------------

def _concern(msg: Message, category: str, description: str) -> dict:
    return {
        "behaviour_id": msg.behaviour_id,
        "message_id": msg.id,
        "category": category,
        "description": description,
    }


def check_leakage(
    messages: list[Message], confirm=None
) -> list[dict]:
    """Flag implementation-detail leaks in behaviour, behaviour-status,
    evidence.claim, and design-result.description fields.

    `confirm` is an optional callable `(text) -> bool` used to adversarially
    confirm regex candidates with a model.  When omitted (or None), the regex
    heuristic alone decides.
    """
    concerns: list[dict] = []
    for msg in messages:
        for description, text in _leak_candidates(msg):
            if confirm is None or confirm(text):
                concerns.append(_concern(msg, "leakage", description))
    return concerns


def _leak_candidates(msg: Message):
    """Yield `(description, text)` for each field the regex flags as a leak."""
    if msg.type == "behaviour":
        text = " ".join(
            str(msg.payload.get(k, "")) for k in ("actor", "outcome", "boundaries")
        )
        if _has_leak(text):
            yield ("behaviour content leaks implementation detail", text)
    elif msg.type == "behaviour-status":
        text = str(msg.payload.get("summary", ""))
        if _has_leak(text):
            yield ("behaviour-status summary leaks implementation detail", text)
    elif msg.type == "evidence":
        for item in msg.payload.get("evidence", []):
            claim = str(item.get("claim", ""))
            if _has_leak(claim):
                yield (f"evidence claim leaks implementation detail: {claim[:80]}", claim)
    elif msg.type == "design-result":
        # The Designer's description must describe the visual direction in plain
        # language.  artifact_ref is deliberately a file path, so it is not
        # checked here.
        text = str(msg.payload.get("description", ""))
        if _has_leak(text):
            yield ("design-result description leaks implementation detail", text)


def check_gamed_expectation(
    new_messages: list[Message], full_history: list[Message]
) -> list[dict]:
    """Flag an expectation whose wording shifted to match a prior evidence claim."""
    if not any(m.type == "expectation" for m in new_messages):
        return []

    # group full history by behaviour_id (already sorted chronologically)
    by_bid: dict[str, list[Message]] = {}
    for m in full_history:
        by_bid.setdefault(m.behaviour_id, []).append(m)

    concerns: list[dict] = []
    for msg in new_messages:
        if msg.type != "expectation":
            continue
        history = by_bid.get(msg.behaviour_id, [])

        # Collect evidence claims that precede this expectation in time.
        prior_claims: dict[str, list[str]] = {}
        for h in history:
            if h.id >= msg.id:
                break
            if h.type == "evidence":
                for item in h.payload.get("evidence", []):
                    eid = str(item.get("expectation_id", ""))
                    prior_claims.setdefault(eid, []).append(str(item.get("claim", "")))

        for item in msg.payload.get("expectations", []):
            eid = str(item.get("id", ""))
            statement = str(item.get("statement", ""))
            for claim in prior_claims.get(eid, []):
                if _similar(statement, claim):
                    concerns.append(
                        _concern(
                            msg,
                            "gamed_expectation",
                            f"expectation {eid} was reworded to match prior evidence",
                        )
                    )
                    break
    return concerns


def check_confidence_mismatch(messages: list[Message]) -> list[dict]:
    """Flag evidence marked 'executed' whose raw_output is empty or trivial."""
    concerns: list[dict] = []
    for msg in messages:
        if msg.type != "evidence":
            continue
        for item in msg.payload.get("evidence", []):
            if item.get("confidence") != "executed":
                continue
            execution = item.get("execution", {})
            raw_output = str(execution.get("raw_output", "")).strip()
            command = str(execution.get("command", "")).strip()
            if not command or len(raw_output) < MIN_RAW_OUTPUT_LEN:
                concerns.append(
                    _concern(
                        msg,
                        "confidence_mismatch",
                        "evidence marked 'executed' but raw_output is empty or trivial",
                    )
                )
    return concerns


# Marker the Designer appends to a design-result description when the review
# loop hit max_loops without approval (see workers/designer.py CAP_NOTE).
DESIGN_CAP_MARKER = "ITERATION CAP REACHED"


def check_systemic_loop(
    new_messages: list[Message], full_history: list[Message]
) -> list[dict]:
    """Roll up behaviours (and design explorations) that repeatedly hit
    max_loops into a single advisory per role."""
    concerns: list[dict] = []

    # Builder: behaviours ending "blocked" (the expectation loop hit max_loops).
    new_blocked = [
        m
        for m in new_messages
        if m.type == "behaviour-status" and m.payload.get("outcome") == "blocked"
    ]
    if new_blocked:
        blocked_bids: set[str] = set()
        all_bids: set[str] = set()
        for m in full_history:
            all_bids.add(m.behaviour_id)
            if m.type == "behaviour-status" and m.payload.get("outcome") == "blocked":
                blocked_bids.add(m.behaviour_id)
        total = len(all_bids)
        if total >= MIN_BEHAVIOURS_FOR_SYSTEMIC and (
            len(blocked_bids) / total >= BLOCKED_RATIO_THRESHOLD
        ):
            concerns.append(
                {
                    "behaviour_id": "",
                    "message_id": "",
                    "category": "systemic_loop",
                    "description": (
                        f"{len(blocked_bids)} of {total} behaviours hit max_loops "
                        "(blocked) — consider reviewing the Builder's configured model"
                    ),
                }
            )

    # Designer: design explorations that hit their review-loop iteration cap.
    def _capped(m: Message) -> bool:
        return (
            m.type == "design-result"
            and DESIGN_CAP_MARKER in str(m.payload.get("description", ""))
        )

    if any(_capped(m) for m in new_messages):
        designs = [m for m in full_history if m.type == "design-result"]
        capped = [m for m in designs if _capped(m)]
        if len(designs) >= MIN_BEHAVIOURS_FOR_SYSTEMIC and (
            len(capped) / len(designs) >= BLOCKED_RATIO_THRESHOLD
        ):
            concerns.append(
                {
                    "behaviour_id": "",
                    "message_id": "",
                    "category": "systemic_loop",
                    "description": (
                        f"the Designer hit its iteration cap on {len(capped)} of "
                        f"{len(designs)} design explorations — consider reviewing "
                        "the Designer's configured model"
                    ),
                }
            )

    return concerns


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= SIMILARITY_THRESHOLD


# ---------------------------------------------------------------------------
# The Sentinel
# ---------------------------------------------------------------------------

class Sentinel:
    """Periodically scans the ledger and emits advisory/warning messages."""

    def __init__(
        self,
        config,
        router,
        relay_dir: Path,
        scan_every: int | None = None,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.config = config
        self.router = router  # used for model-backed leak confirmation
        self.relay_dir = relay_dir
        sentinel_cfg = config.roles.get("sentinel")
        self.scan_every = (
            scan_every
            if scan_every is not None
            else (sentinel_cfg.scan_every if sentinel_cfg else DEFAULT_SCAN_EVERY)
        )
        self.poll_interval = poll_interval
        self.cursor: str | None = None

    def run(self) -> None:
        """Block forever, scanning on the configured cadence."""
        while True:
            if self._new_count() >= self.scan_every:
                self.scan_once()
            time.sleep(self.poll_interval)

    def _new_count(self) -> int:
        full = list_ledger(self.relay_dir)
        if self.cursor is None:
            return len(full)
        return sum(1 for m in full if m.id > self.cursor)

    def scan_once(self) -> list[Message]:
        """Run one scan over new messages and emit advisories for any findings."""
        full = list_ledger(self.relay_dir)
        if not full:
            return []

        new = full if self.cursor is None else [m for m in full if m.id > self.cursor]
        if not new:
            return []

        concerns: list[dict] = []
        concerns += check_leakage(new, confirm=self._confirm_leak)
        concerns += check_gamed_expectation(new, full)
        concerns += check_confidence_mismatch(new)
        concerns += check_systemic_loop(new, full)

        self.cursor = full[-1].id

        if not concerns:
            return []
        return self._emit(concerns)

    def _sentinel_model(self) -> str | None:
        sentinel = self.config.roles.get("sentinel")
        if sentinel is None or sentinel.model is None:
            return None
        return sentinel.model

    def _confirm_leak(self, text: str) -> bool:
        """Adversarially confirm a regex leak candidate with the Sentinel's model.

        Falls back to trusting the regex heuristic (returning True) when no
        model is configured, the router is absent, or the model call fails —
        so a model outage never silently drops a flag the heuristic caught.
        """
        model = self._sentinel_model()
        if model is None or self.router is None:
            return True

        prompt = LEAK_PROMPT.format(text=text)
        try:
            verdict = call_for_schema(
                self.router,
                model,
                prompt,
                LeakVerdict,
                overrides=self.config.capabilities.model_overrides,
                label="leak verdict",
            )
        except ModelCallError as exc:
            print(f"[sentinel] leak confirmation failed, trusting heuristic: {exc}")
            return True
        return bool(verdict.get("leak"))

    def _emit(self, concerns: list[dict]) -> list[Message]:
        msg = Message(
            id=str(ULID()),
            type="advisory",
            from_role="sentinel",
            to_role="owner",
            behaviour_id=str(ULID()),
            created_at=datetime.now(timezone.utc),
            schema_version=1,
            payload={"concerns": concerns, "severity": "advisory"},
        )
        write_message(msg, self.relay_dir)
        print(f"[sentinel] emitted advisory with {len(concerns)} concern(s)")
        return [msg]


def sentinel_summary(relay_dir: Path) -> dict[tuple[str, str], int]:
    """Count open Sentinel flags by (severity, category)."""
    counts: dict[tuple[str, str], int] = {}
    for m in list_ledger(relay_dir):
        if m.type not in ("advisory", "warning"):
            continue
        severity = str(m.payload.get("severity", "advisory"))
        for concern in m.payload.get("concerns", []):
            key = (severity, str(concern.get("category", "unknown")))
            counts[key] = counts.get(key, 0) + 1
    return counts
