"""Ledger — permanent, append-only message store.

Every message is written once as a JSON file under `.brigade/ledger/<ulid>.json`.
Writes are atomic (temp file + rename).  Nothing in this module ever modifies
or deletes a committed ledger entry.
"""

from __future__ import annotations

import json
from pathlib import Path

from brigade.messages.models import Message
from brigade.messages.validator import validate


def write_message(message: Message, brigade_dir: Path) -> Path:
    """Validate *message* and write it atomically to the ledger.

    Returns the path of the committed ledger file.
    """
    validate(message)

    ledger_dir = brigade_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    payload = message.model_dump(mode="json")

    # Atomic write: temp file in same directory, then os.rename
    tmp_path = ledger_dir / f".{message.id}.tmp"
    final_path = ledger_dir / f"{message.id}.json"

    tmp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp_path.rename(final_path)

    return final_path


def read_message(message_id: str, brigade_dir: Path) -> Message:
    """Read and reconstruct a `Message` from the ledger by its ULID."""
    path = brigade_dir / "ledger" / f"{message_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"No ledger entry for message {message_id}")

    data = json.loads(path.read_text(encoding="utf-8"))
    return Message.model_validate(data)


def list_ledger(
    brigade_dir: Path, *, behaviour_id: str | None = None
) -> list[Message]:
    """Return every message in the ledger, chronologically (by ULID).

    When *behaviour_id* is given, only messages belonging to that behaviour
    are returned.
    """
    ledger_dir = brigade_dir / "ledger"
    if not ledger_dir.is_dir():
        return []

    messages: list[Message] = []
    for path in sorted(ledger_dir.glob("*.json")):
        try:
            msg = Message.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if behaviour_id is None or msg.behaviour_id == behaviour_id:
                messages.append(msg)
        except Exception:
            # skip corrupt / non-message files silently —
            # the ledger should never contain them, but be defensive
            continue

    return messages
