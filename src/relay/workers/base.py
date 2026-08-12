"""Generic worker loop and helpers shared by all roles."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError as PydanticValidationError
from ulid import ULID

from relay.capabilities import resolve_capabilities
from relay.config import Config
from relay.llm import ModelRouter
from relay.messages import Message, validate
from relay.personas import load_persona
from relay.storage import consume, deliver, list_inbox

POLL_INTERVAL = 0.5
MAX_SCHEMA_RETRIES = 3


class WorkerError(Exception):
    """Raised when a worker cannot produce a valid reply."""


def build_reply(
    incoming: Message,
    to_role: str,
    msg_type: str,
    payload: dict,
    behaviour_id: str | None = None,
) -> Message:
    """Assemble a reply message.

    All envelope fields are set by code — the model only supplies payload
    content.  `from_role` is the incoming message's recipient (i.e. this
    worker's own role).
    """
    return Message(
        id=str(ULID()),
        type=msg_type,
        from_role=incoming.to_role,
        to_role=to_role,
        behaviour_id=behaviour_id or incoming.behaviour_id,
        reply_to=incoming.id,
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        payload=payload,
    )


class RoleWorker:
    """Role-agnostic worker loop.

    Subclasses override `process()` to turn an incoming message into a reply.
    The loop mechanics — polling, consuming, prompting, schema-retry, envelope
    assembly, validation, delivery — are identical across roles.
    """

    role: str = ""

    def __init__(
        self,
        config: Config,
        router: ModelRouter,
        relay_dir: Path,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.config = config
        self.router = router
        self.relay_dir = relay_dir
        self.poll_interval = poll_interval
        self.persona = load_persona(self.role, relay_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Block forever, polling the inbox and processing messages."""
        while True:
            self.run_once()
            time.sleep(self.poll_interval)

    def run_once(self) -> bool:
        """Process all currently-pending inbox messages once.

        Returns True if at least one message was processed.
        """
        ids = list_inbox(self.role, self.relay_dir)
        if not ids:
            return False
        for msg_id in ids:
            msg = consume(self.role, msg_id, self.relay_dir)
            self._dispatch(msg)
        return True

    def process(self, msg: Message) -> Message | None:
        """Role-specific handling.  Return the reply message, or None to skip."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Internal machinery
    # ------------------------------------------------------------------

    def _dispatch(self, msg: Message) -> None:
        print(f"[{self.role}] consuming {msg.type} ({msg.id})")
        reply = self.process(msg)
        if reply is None:
            return
        validate(reply)
        deliver(reply, self.relay_dir)
        print(f"[{self.role}] produced {reply.type} → {reply.to_role} ({reply.id})")

    def _model_name(self) -> str:
        model = self.config.roles.get(self.role)
        if model is None or model.model is None:
            raise WorkerError(
                f"No model configured for role '{self.role}' — set "
                f"[roles.{self.role}].model in config.toml"
            )
        return model.model

    def _call_model(
        self, prompt: str, schema: type[BaseModel], output_label: str
    ) -> dict:
        """Call the model and validate its output against *schema*.

        Uses JSON mode when the model's capability table entry allows it.
        On parse/schema failure, feeds the specific error back to the model
        and retries, up to MAX_SCHEMA_RETRIES times.
        """
        model = self._model_name()
        caps = resolve_capabilities(
            model, self.config.capabilities.model_overrides
        )
        json_mode = caps.structured_output in ("strict", "loose")

        errors: list[str] = []
        for _ in range(MAX_SCHEMA_RETRIES):
            full_prompt = prompt
            if errors:
                full_prompt += (
                    "\n\nYour previous response was invalid: "
                    + "; ".join(errors)
                    + "\nCorrect it and respond again with JSON matching the "
                      "required schema."
                )

            raw = self.router.complete(model, full_prompt, json_mode=json_mode)

            try:
                data = self._parse_json(raw)
                validated = schema.model_validate(data)
                return validated.model_dump()
            except (ValueError, PydanticValidationError) as exc:
                errors.append(self._format_error(exc))

        raise WorkerError(
            f"Failed to produce a valid {output_label} after "
            f"{MAX_SCHEMA_RETRIES} attempts: {'; '.join(errors)}"
        )

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        # strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return parsed

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, PydanticValidationError):
            return "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
        return str(exc)
