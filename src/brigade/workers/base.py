"""Generic worker loop and helpers shared by all roles."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel
from ulid import ULID

from brigade.config import Config
from brigade.llm import ModelRouter
from brigade.messages import Message, validate
from brigade.model_calls import ModelCallError, call_for_schema
from brigade.personas import load_persona
from brigade.storage import complete, consume, deliver, list_inbox, recover_in_progress

POLL_INTERVAL = 0.5


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
        brigade_dir: Path,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.config = config
        self.router = router
        self.brigade_dir = brigade_dir
        self.poll_interval = poll_interval
        self.persona = load_persona(self.role, brigade_dir)
        self.logger = logging.getLogger(self.role)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Block forever, polling the inbox and processing messages.

        Recovers any messages stranded by a previous unclean shutdown, then
        loops.  A per-message failure is logged and left in `in-progress/` for
        the next startup to recover; a failure in the poll loop itself is logged
        before the thread exits; and worker exit is always logged loudly.
        """
        for msg_id in recover_in_progress(self.role, self.brigade_dir):
            self.logger.warning(
                "recovered message %s from a previous unclean shutdown",
                msg_id,
                extra={"event": "message_recovered", "message_id": msg_id},
            )
        self.logger.info("worker started", extra={"event": "worker_start"})
        try:
            while True:
                self.run_once()
                time.sleep(self.poll_interval)
        except Exception:
            self.logger.exception(
                "worker loop crashed", extra={"event": "worker_crashed"}
            )
        finally:
            self.logger.error("worker exiting", extra={"event": "worker_exit"})

    def run_once(self) -> bool:
        """Process all currently-pending inbox messages once.

        Returns True if at least one message was processed.
        """
        ids = list_inbox(self.role, self.brigade_dir)
        if not ids:
            return False
        for msg_id in ids:
            try:
                msg = consume(self.role, msg_id, self.brigade_dir)
                self._dispatch(msg)
                complete(self.role, msg_id, self.brigade_dir)  # only on success
            except Exception:
                self.logger.exception(
                    "error processing message %s", msg_id,
                    extra={"event": "worker_error", "message_id": msg_id},
                )
                # pointer stays in in-progress/ — recovered on next startup
        return True

    def process(self, msg: Message) -> Message | None:
        """Role-specific handling.  Return the reply message, or None to skip."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Internal machinery
    # ------------------------------------------------------------------

    def _dispatch(self, msg: Message) -> None:
        self.logger.info(
            "consuming %s (%s)", msg.type, msg.id,
            extra={
                "behaviour_id": msg.behaviour_id,
                "event": "consuming",
                "message_type": msg.type,
                "message_id": msg.id,
                "loop_count": msg.payload.get("loop_count"),
                "max_loops": msg.payload.get("max_loops"),
            },
        )
        reply = self.process(msg)
        if reply is None:
            return
        validate(reply)
        deliver(reply, self.brigade_dir)
        self.logger.info(
            "produced %s → %s (%s)", reply.type, reply.to_role, reply.id,
            extra={
                "behaviour_id": reply.behaviour_id,
                "event": "produced",
                "message_type": reply.type,
                "message_id": reply.id,
                "to_role": reply.to_role,
            },
        )

    def _log_harness(self, behaviour_id: str, result, duration: float) -> Path:
        """Persist full harness output to a log file and log a one-line summary."""
        logs_dir = self.brigade_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"harness-{self.role}-{str(ULID())}.log"
        path.write_text(
            f"command: {' '.join(result.command)}\n"
            f"exit_code: {result.exit_code}\n"
            f"duration: {duration:.1f}s\n\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"\n--- stderr ---\n{result.stderr}\n"
        )
        self.logger.info(
            "harness exited %s in %.1fs — %s",
            result.exit_code, duration, path,
            extra={
                "behaviour_id": behaviour_id,
                "event": "harness_end",
                "duration_s": duration,
            },
        )
        return path

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
        try:
            return call_for_schema(
                self.router,
                model,
                prompt,
                schema,
                role=self.role,
                overrides=self.config.capabilities.model_overrides,
                label=output_label,
                timeout=self.config.role_timeout(self.role),
            )
        except ModelCallError as exc:
            raise WorkerError(str(exc)) from exc
