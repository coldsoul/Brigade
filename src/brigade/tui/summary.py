"""Ledger summarization helpers for the TUI."""

from __future__ import annotations

from pathlib import Path

from brigade.storage import list_ledger


def summarize_behaviours(brigade_dir: Path) -> list[dict]:
    """Group the ledger by `behaviour_id` and summarize each behaviour's state.

    Returns a list of dicts, most-recent-activity first, each with:
    `behaviour_id`, `status` (in-progress/solved/blocked/partial), `stage`
    (the last message type), `next_role`, `loop`, and `last_activity`.
    """
    by_bid: dict[str, list] = {}
    for msg in list_ledger(brigade_dir):
        by_bid.setdefault(msg.behaviour_id, []).append(msg)

    summaries: list[dict] = []
    for bid, msgs in by_bid.items():
        last = msgs[-1]

        status = "in-progress"
        for msg in msgs:
            if msg.type == "behaviour-status":
                outcome = msg.payload.get("outcome")
                if outcome in ("solved", "blocked", "partial"):
                    status = outcome

        loop = None
        for msg in reversed(msgs):
            if msg.type in ("expectation", "verdict"):
                loop = msg.payload.get("loop_count")
                break

        summaries.append(
            {
                "behaviour_id": bid,
                "status": status,
                "stage": last.type,
                "next_role": last.to_role,
                "loop": loop,
                "last_activity": last.created_at,
            }
        )

    summaries.sort(key=lambda s: s["last_activity"], reverse=True)
    return summaries
