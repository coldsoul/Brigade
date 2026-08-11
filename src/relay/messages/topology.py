"""Fixed topology table — never varies between projects.

Every edge in the Relay Method, the message types legal on that edge,
and which edges are mailbox-routed vs live-chat.
"""

# (from_role, to_role) → set of allowed message types
TOPOLOGY: dict[tuple[str, str], set[str]] = {
    ("interpreter", "analyst"): {"behaviour-to-implement"},
    ("analyst", "examiner"): {"behaviour"},
    ("examiner", "builder"): {"expectation", "verdict"},
    ("builder", "examiner"): {"evidence"},
    ("examiner", "analyst"): {"behaviour-status"},
    ("analyst", "interpreter"): {"behaviour-status"},
}

# Owner ↔ Interpreter types — valid edges but never mailbox-routed.
# These represent the live chat, not a file-polled queue.
OWNER_INTERPRETER_TYPES: set[str] = {
    "problem",
    "clarification",
    "roadmap",
    "roadmap-verdict",
    "increment",
    "continue-query",
    "feedback",
    "result",
    "question",
}

# Edges that go through the mailbox system (deliver/consume).
# Owner ↔ Interpreter edges do NOT appear here.
MAILBOX_ROUTED_EDGES: set[tuple[str, str]] = set(TOPOLOGY.keys())


def is_owner_interpreter_edge(from_role: str, to_role: str) -> bool:
    """True if this is an Owner ↔ Interpreter edge (live chat, not mailbox)."""
    return {from_role, to_role} == {"owner", "interpreter"}
