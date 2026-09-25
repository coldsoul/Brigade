"""Bridge between Python logging and the Textual TUI.

A `logging.Handler` that converts the structured log records (the ones carrying
an `event` extra field) into `RoleEvent`/`UsageEvent` messages and posts them to
the running Textual app, safely, from the worker threads.
"""

from __future__ import annotations

import logging

from brigade.tui.events import RoleEvent, UsageEvent


class TUILogHandler(logging.Handler):
    """Convert structured log records into TUI events."""

    def __init__(self, app):
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        event_type = getattr(record, "event", None)
        if event_type is None:
            # Not a structured event — leave it to the console/file handlers.
            return

        if event_type == "usage":
            model = getattr(record, "model", "")
            event = UsageEvent(
                role=record.name,
                provider=model.split("/", 1)[0] if model else record.name,
                model=model,
                prompt_tokens=getattr(record, "prompt_tokens", 0),
                completion_tokens=getattr(record, "completion_tokens", 0),
                ts=record.created,
            )
        else:
            event = RoleEvent(
                role=record.name,
                event=event_type,
                behaviour_id=getattr(record, "behaviour_id", None),
                message_type=getattr(record, "message_type", None),
                message_id=getattr(record, "message_id", None),
                to_role=getattr(record, "to_role", None),
                loop_count=getattr(record, "loop_count", None),
                max_loops=getattr(record, "max_loops", None),
                duration_s=getattr(record, "duration_s", None),
                concern_count=getattr(record, "concern_count", None),
                restart_count=getattr(record, "restart_count", None),
                text=record.getMessage(),
                level=record.levelname,
                ts=record.created,
            )

        self.app.call_from_thread(self.app.post_message, event)
