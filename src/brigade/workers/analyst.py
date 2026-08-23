"""Analyst worker — needs → behaviours, and status re-authoring."""

from __future__ import annotations

from brigade.messages import (
    BehaviourPayload,
    BehaviourStatusPayload,
    Message,
)
from brigade.workers.base import RoleWorker, build_reply


class AnalystWorker(RoleWorker):
    role = "analyst"

    def process(self, msg: Message) -> Message | None:
        if msg.type == "behaviour-to-implement":
            return self._handle_behaviour_to_implement(msg)
        if msg.type == "behaviour-status":
            return self._handle_behaviour_status(msg)
        return None

    def _handle_behaviour_to_implement(self, msg: Message) -> Message:
        prompt = self._build_prompt(
            "Turn the following need into an observable behaviour.\n\n"
            f"NEED:\n{msg.payload.get('text', '')}"
        )
        payload = self._call_model(prompt, BehaviourPayload, "behaviour")
        return build_reply(msg, "examiner", "behaviour", payload)

    def _handle_behaviour_status(self, msg: Message) -> Message:
        # Re-author (not forward verbatim) a further-stripped status for the
        # Interpreter — the Analyst's version must not reference expectations.
        prompt = self._build_prompt(
            "Re-author the following behaviour status for the human Owner.\n"
            "Strip out any reference to expectations, tests, or implementation "
            "detail; keep only the plain-language outcome and summary.\n\n"
            f"INCOMING STATUS:\n{msg.payload.get('summary', '')}"
        )
        payload = self._call_model(
            prompt, BehaviourStatusPayload, "behaviour-status"
        )
        # Preserve the behaviour_id from the incoming message's payload
        payload["behaviour_id"] = msg.behaviour_id
        return build_reply(msg, "interpreter", "behaviour-status", payload)

    def _build_prompt(self, instruction: str) -> str:
        return f"{self.persona}\n\n{instruction}\n\nRespond with JSON only."
