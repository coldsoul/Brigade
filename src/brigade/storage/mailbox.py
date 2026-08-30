"""Mailbox — transient queue state layered on top of the ledger.

Each mailbox inbox contains pointer files (empty marker files) referencing
unconsumed ledger entries.  Claiming a message moves its pointer from `inbox/`
to `in-progress/`; the pointer is only deleted by `complete()` once the reply
has been delivered, so a crash mid-processing leaves a recoverable trace.
The ledger entry itself persists forever.
"""

from __future__ import annotations

import os
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
    """Claim a message from *role*'s inbox for processing.

    Atomically moves the inbox pointer into `in-progress/` rather than deleting
    it, so a crash before the reply is delivered leaves a recoverable trace.
    The pointer is cleared only by `complete()`, after delivery succeeds.
    Raises `FileNotFoundError` if the pointer does not exist.
    """
    inbox_pointer = brigade_dir / "mailboxes" / role / "inbox" / message_id
    if not inbox_pointer.is_file():
        raise FileNotFoundError(
            f"No message '{message_id}' in {role}'s inbox"
        )

    in_progress_dir = brigade_dir / "mailboxes" / role / "in-progress"
    in_progress_dir.mkdir(parents=True, exist_ok=True)
    in_progress_pointer = in_progress_dir / message_id
    os.replace(inbox_pointer, in_progress_pointer)  # atomic on same filesystem

    return read_message(message_id, brigade_dir)  # unchanged: read from ledger


def complete(role: str, message_id: str, brigade_dir: Path) -> None:
    """Mark a claimed message as fully processed, removing its in-progress pointer.

    Called only after the worker has successfully delivered its reply (or
    deliberately produced no reply).  Safe to call if the pointer is already
    gone.
    """
    in_progress_pointer = brigade_dir / "mailboxes" / role / "in-progress" / message_id
    in_progress_pointer.unlink(missing_ok=True)


def recover_in_progress(role: str, brigade_dir: Path) -> list[str]:
    """Move any messages stranded in `in-progress/` back into the inbox.

    Called once when a worker starts.  A pointer in `in-progress/` means a
    previous run claimed the message but never completed it (crash, kill, bug).
    Returns the list of recovered message ids so the caller can log them.
    """
    in_progress_dir = brigade_dir / "mailboxes" / role / "in-progress"
    if not in_progress_dir.is_dir():
        return []

    inbox_dir = brigade_dir / "mailboxes" / role / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    recovered = []
    for pointer in in_progress_dir.iterdir():
        os.replace(pointer, inbox_dir / pointer.name)
        recovered.append(pointer.name)
    return recovered
