"""Message validator — enforces topology, type, and payload schema."""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from relay.messages.models import PAYLOAD_MODEL_BY_TYPE, Message
from relay.messages.topology import (
    OWNER_INTERPRETER_TYPES,
    SENTINEL_TYPES,
    TOPOLOGY,
    is_owner_interpreter_edge,
)


class ValidationError(Exception):
    """Structured validation failure — suitable for feeding back to a model."""

    def __init__(self, message: str, details: dict):
        self.message = message
        self.details = details
        super().__init__(message)

    def __repr__(self) -> str:
        return f"ValidationError({self.message!r}, details={self.details!r})"


def validate(message: Message) -> None:
    """Check that *message* obeys the topology, type, and payload rules.

    Raises `ValidationError` on failure.  Returns `None` on success.
    Checks are performed in order so the first problem found is reported.
    """
    edge = (message.from_role, message.to_role)
    msg_type = message.type

    # Sentinel wildcard: the Sentinel may send advisory/warning to any role.
    if message.from_role == "sentinel":
        if msg_type not in SENTINEL_TYPES:
            raise ValidationError(
                f"Sentinel may only send {sorted(SENTINEL_TYPES)}, not '{msg_type}'",
                {
                    "type": msg_type,
                    "allowed_types": sorted(SENTINEL_TYPES),
                },
            )
        _validate_payload(msg_type, message)
        return

    # 1. Check the edge exists
    allowed_types = TOPOLOGY.get(edge)

    if allowed_types is None:
        # Maybe it's an Owner ↔ Interpreter edge
        if msg_type in OWNER_INTERPRETER_TYPES and is_owner_interpreter_edge(
            message.from_role, message.to_role
        ):
            allowed_types = {msg_type}
        else:
            raise ValidationError(
                f"No edge {message.from_role} → {message.to_role}",
                {
                    "from_role": message.from_role,
                    "to_role": message.to_role,
                    "valid_edges": sorted(
                        f"{f}→{t}" for f, t in TOPOLOGY
                    ),
                    "owner_interpreter_types": sorted(OWNER_INTERPRETER_TYPES),
                },
            )

    # 2. Check the type is legal on this edge
    if msg_type not in allowed_types:
        raise ValidationError(
            f"Type '{msg_type}' is not allowed on edge "
            f"{message.from_role} → {message.to_role}",
            {
                "type": msg_type,
                "allowed_types": sorted(allowed_types),
                "edge": f"{message.from_role}→{message.to_role}",
            },
        )

    # 3. Validate the payload against the type's schema
    _validate_payload(msg_type, message)


def _validate_payload(msg_type: str, message: Message) -> None:
    """Validate `message.payload` against the schema registered for `msg_type`."""
    payload_model = PAYLOAD_MODEL_BY_TYPE.get(msg_type)
    if payload_model is None:
        raise ValidationError(
            f"Unknown message type: {msg_type}",
            {"type": msg_type, "known_types": sorted(PAYLOAD_MODEL_BY_TYPE)},
        )

    try:
        payload_model.model_validate(message.payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"Invalid payload for type '{msg_type}'",
            {
                "type": msg_type,
                "errors": [
                    {"loc": list(err["loc"]), "msg": err["msg"]}
                    for err in exc.errors()
                ],
            },
        ) from exc
