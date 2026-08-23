"""Mailbox — transient queue state layered on top of the ledger.

Each mailbox inbox contains pointer files (empty marker files) referencing
unconsumed ledger entries.  Once consumed, the pointer is deleted but the
ledger entry persists forever.
"""

from __future__ import annotations

from pathlib import Path

from brigade.messages.models import Message
from brigade.storage.ledger import read_message, write_message


def deliver(message: Message, brigade_dir: Path) -> None:
    """Write *message* to the ledger and drop an inbox pointer for the recipient.

    Owner ↔ Interpreter messages should call `write_message` directly instead.
    """
    write_message(message, brigade_dir)

    inbox_dir = brigade_dir / "mailboxes" / message.to_role / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    pointer = inbox_dir / message.id
    pointer.touch()


def list_inbox(role: str, brigade_dir: Path) -> list[str]:
    """Return pending message IDs for *role*, oldest first (ULID sort)."""
    inbox_dir = brigade_dir / "mailboxes" / role / "inbox"
    if not inbox_dir.is_dir():
        return []

    return sorted(
        p.name
        for p in inbox_dir.iterdir()
        if p.is_file()
    )


def consume(role: str, message_id: str, brigade_dir: Path) -> Message:
    """Claim a message from *role*'s inbox.

    Reads the full message from the ledger, removes the inbox pointer,
    and returns the message.  Raises `FileNotFoundError` if the pointer
    does not exist.
    """
    pointer = brigade_dir / "mailboxes" / role / "inbox" / message_id
    if not pointer.is_file():
        raise FileNotFoundError(
            f"No message '{message_id}' in {role}'s inbox"
        )

    message = read_message(message_id, brigade_dir)
    pointer.unlink()
    return message
