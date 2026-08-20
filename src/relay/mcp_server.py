"""Interpreter MCP server — the bridge between the coding harness and Relay.

This is a stdio MCP server spawned as a subprocess of the harness.  It exposes
the three tools the Interpreter uses: dispatch_behaviour, check_status, and
log_conversation.  The relay directory is resolved from the process's working
directory (the project directory the harness is running in).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from relay.interpreter import (
    check_status_impl,
    dispatch_behaviour_impl,
    log_conversation_impl,
)
from relay.paths import find_relay_dir

mcp = MCPServer(
    name="relay",
    version="0.1.0",
    instructions="Relay Interpreter tools — dispatch behaviours, check status, "
    "and log the Owner↔Interpreter conversation.",
)

# Tracks the previous conversation message id so log_conversation calls chain
# via reply_to, keeping the whole conversation replayable in the ledger.
_last_conversation_id: str | None = None


def _require_relay_dir():
    relay_dir = find_relay_dir()
    if relay_dir is None:
        raise RuntimeError(
            "not a relay project — no `.relay/` directory found. Run "
            "`relay init` in this directory first."
        )
    return relay_dir


@mcp.tool()
def dispatch_behaviour(text: str) -> dict:
    """Send a behaviour downward for implementation. Returns immediately."""
    return dispatch_behaviour_impl(_require_relay_dir(), text)


@mcp.tool()
def check_status(behaviour_id: str) -> dict:
    """Check the status of a dispatched behaviour. Non-blocking poll."""
    return check_status_impl(_require_relay_dir(), behaviour_id)


@mcp.tool()
def log_conversation(type: str, text: str) -> None:
    """Record an Owner↔Interpreter message in the permanent ledger."""
    global _last_conversation_id
    _last_conversation_id = log_conversation_impl(
        _require_relay_dir(), type, text, _last_conversation_id
    )


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    mcp.run(transport="stdio")
