"""Interpreter tool logic — the core of the MCP server, kept free of MCP glue.

These functions are pure (stateless) and take `relay_dir` explicitly so they
can be unit-tested without a running MCP server.  The MCP server layer resolves
the relay directory and wraps them as tools.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

from relay.messages import Message
from relay.storage import consume, deliver, list_inbox, read_message, write_message

# Owner ↔ Interpreter message types → (from_role, to_role).  Inferred from the
# type: "problem" comes from the Owner, "roadmap"/"increment"/"result" go to the
# Owner, and so on.
CONVERSATION_DIRECTION: dict[str, tuple[str, str]] = {
    "problem": ("owner", "interpreter"),
    "clarification": ("interpreter", "owner"),
    "roadmap": ("interpreter", "owner"),
    "roadmap-verdict": ("owner", "interpreter"),
    "increment": ("interpreter", "owner"),
    "continue-query": ("interpreter", "owner"),
    "feedback": ("owner", "interpreter"),
    "result": ("interpreter", "owner"),
    "question": ("interpreter", "owner"),
}


def dispatch_behaviour_impl(relay_dir: Path, text: str) -> dict:
    """Construct, validate, and deliver a `behaviour-to-implement` message.

    Returns the new behaviour_id immediately — never blocks.
    """
    behaviour_id = str(ULID())
    msg = Message(
        id=str(ULID()),
        type="behaviour-to-implement",
        from_role="interpreter",
        to_role="analyst",
        behaviour_id=behaviour_id,
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        payload={"text": text},
    )
    deliver(msg, relay_dir)
    return {"behaviour_id": behaviour_id}


def check_status_impl(relay_dir: Path, behaviour_id: str) -> dict:
    """Poll the interpreter inbox for a `behaviour-status` for *behaviour_id*.

    Consumes (clears the pointer) when found, keeping the ledger entry.
    Returns {"outcome": "pending", "summary": None} when not yet present.
    """
    for msg_id in list_inbox("interpreter", relay_dir):
        msg = read_message(msg_id, relay_dir)
        if msg.type == "behaviour-status" and msg.payload.get("behaviour_id") == behaviour_id:
            consume("interpreter", msg_id, relay_dir)
            return {
                "outcome": msg.payload.get("outcome", "pending"),
                "summary": msg.payload.get("summary"),
            }
    return {"outcome": "pending", "summary": None}


def log_conversation_impl(
    relay_dir: Path, msg_type: str, text: str, reply_to: str | None = None
) -> str:
    """Write a Owner↔Interpreter message directly to the ledger (no inbox).

    Returns the new message id (so the caller can chain reply_to).
    """
    from_role, to_role = CONVERSATION_DIRECTION[msg_type]
    msg = Message(
        id=str(ULID()),
        type=msg_type,
        from_role=from_role,
        to_role=to_role,
        behaviour_id=str(ULID()),
        reply_to=reply_to,
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        payload={"text": text},
    )
    write_message(msg, relay_dir)
    return msg.id
