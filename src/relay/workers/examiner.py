"""Examiner worker — behaviours → expectations, and evidence → verdict/status."""

from __future__ import annotations

from pydantic import BaseModel

from relay.messages import Message, UnmetExpectation
from relay.workers.base import RoleWorker, build_reply


class ExpectationDraft(BaseModel):
    """What the model produces for a behaviour — code adds loop bookkeeping."""

    expectations: list[dict]
    integration_expectation: str


class EvidenceEvaluation(BaseModel):
    """What the model produces when judging evidence — code decides the outcome."""

    satisfied: list[str]
    unmet: list[UnmetExpectation]
    summary: str


class ExaminerWorker(RoleWorker):
    role = "examiner"

    def process(self, msg: Message) -> Message | None:
        if msg.type == "behaviour":
            return self._handle_behaviour(msg)
        if msg.type == "evidence":
            return self._handle_evidence(msg)
        return None

    # ------------------------------------------------------------------
    # behaviour → expectation
    # ------------------------------------------------------------------

    def _handle_behaviour(self, msg: Message) -> Message:
        prompt = self._build_prompt(
            "Decompose the following behaviour into precise, checkable "
            "expectations (E1..En) plus one integration expectation.\n\n"
            f"BEHAVIOUR:\nactor={msg.payload.get('actor', '')}\n"
            f"outcome={msg.payload.get('outcome', '')}\n"
            f"boundaries={msg.payload.get('boundaries', '')}"
        )
        draft = self._call_model(prompt, ExpectationDraft, "expectation")

        payload = {
            "expectations": draft["expectations"],
            "integration_expectation": draft["integration_expectation"],
            "loop_count": 0,
            "max_loops": self.config.project.max_loops,
        }
        return build_reply(msg, "builder", "expectation", payload)

    # ------------------------------------------------------------------
    # evidence → verdict / behaviour-status
    # ------------------------------------------------------------------

    def _handle_evidence(self, msg: Message) -> Message:
        prompt = self._build_prompt(
            "Judge the following evidence against the expectations. Use the "
            "adversarial checklist: do the numbers add up, did it dodge an edge "
            "case, what input would break this, and does narrative evidence "
            "clear a stricter bar than executed evidence.\n\n"
            f"EVIDENCE:\n{msg.payload}"
        )
        evaluation = self._call_model(
            prompt, EvidenceEvaluation, "evidence evaluation"
        )

        current_loop = self._current_loop_count(msg)
        max_loops = self.config.project.max_loops
        satisfied = evaluation["satisfied"]
        unmet = evaluation["unmet"]

        if not unmet:
            payload = {
                "behaviour_id": msg.behaviour_id,
                "outcome": "solved",
                "summary": evaluation["summary"],
            }
            return build_reply(msg, "analyst", "behaviour-status", payload)

        if current_loop >= max_loops:
            payload = {
                "behaviour_id": msg.behaviour_id,
                "outcome": "blocked",
                "summary": evaluation["summary"],
            }
            return build_reply(msg, "analyst", "behaviour-status", payload)

        payload = {
            "satisfied": satisfied,
            "unmet": unmet,
            "loop_count": current_loop + 1,
            "escalate": False,
        }
        return build_reply(msg, "builder", "verdict", payload)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _current_loop_count(self, msg: Message) -> int:
        """Recover the current loop count from the reply chain in the ledger.

        Evidence does not carry loop_count; it replies to an `expectation` or a
        `verdict`, which does.  If the referenced message is missing, assume 0.
        """
        if not msg.reply_to:
            return 0

        from relay.storage import read_message

        try:
            referenced = read_message(msg.reply_to, self.relay_dir)
        except FileNotFoundError:
            return 0

        return referenced.payload.get("loop_count", 0)

    def _build_prompt(self, instruction: str) -> str:
        return f"{self.persona}\n\n{instruction}\n\nRespond with JSON only."
