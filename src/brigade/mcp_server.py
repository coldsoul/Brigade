"""Interpreter MCP server — the bridge between the coding harness and Brigade.

This is a stdio MCP server spawned as a subprocess of the harness.  It exposes
the three tools the Interpreter uses: dispatch_behaviour, check_status, and
log_conversation.  The brigade directory is resolved from the process's working
directory (the project directory the harness is running in).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from brigade.interpreter import (
    check_design_status_impl,
    check_status_impl,
    dispatch_behaviour_impl,
    dispatch_design_request_impl,
    log_conversation_impl,
)
from brigade.paths import find_brigade_dir

mcp = MCPServer(
    name="brigade",
    version="0.1.0",
    instructions="Brigade Interpreter tools — dispatch behaviours, check status, "
    "and log the Owner↔Interpreter conversation.",
)

# Tracks the previous conversation message id so log_conversation calls chain
# via reply_to, keeping the whole conversation replayable in the ledger.
_last_conversation_id: str | None = None


def _require_brigade_dir():
    brigade_dir = find_brigade_dir()
    if brigade_dir is None:
        raise RuntimeError(
            "not a brigade project — no `.brigade/` directory found. Run "
            "`brigade init` in this directory first."
        )
    return brigade_dir


@mcp.tool()
def dispatch_behaviour(text: str) -> dict:
    """Send a behaviour downward for implementation. Returns immediately."""
    return dispatch_behaviour_impl(_require_brigade_dir(), text)


@mcp.tool()
def check_status(behaviour_id: str) -> dict:
    """Check the status of a dispatched behaviour. Non-blocking poll."""
    return check_status_impl(_require_brigade_dir(), behaviour_id)


@mcp.tool()
def dispatch_design_request(text: str) -> dict:
    """Send a design request for visual exploration. Returns immediately."""
    return dispatch_design_request_impl(_require_brigade_dir(), text)


@mcp.tool()
def check_design_status(behaviour_id: str) -> dict:
    """Check the status of a dispatched design request. Non-blocking poll."""
    return check_design_status_impl(_require_brigade_dir(), behaviour_id)


@mcp.tool()
def log_conversation(type: str, text: str) -> None:
    """Record an Owner↔Interpreter message in the permanent ledger."""
    global _last_conversation_id
    _last_conversation_id = log_conversation_impl(
        _require_brigade_dir(), type, text, _last_conversation_id
    )


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    mcp.run(transport="stdio")
