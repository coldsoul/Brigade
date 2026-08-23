"""Review-adapter interface and implementations.

The Designer's core logic depends only on the `ReviewAdapter` protocol, never on
a specific review tool.  Two implementations ship:

- `LavishAdapter` — opens the artifact via lavish-axi and flattens the
  annotations into feedback text.
- `BasicAdapter` — opens the artifact in the OS browser and reads feedback from
  the `brigade up` terminal (always available, no extra dependency).

`select_review_adapter` is the only place in the Designer that knows lavish
exists.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Protocol

APPROVED = "approved"


class ReviewAdapter(Protocol):
    def review(self, artifact_path: str) -> str:
        """Block until the Owner decides.

        Returns the literal string `"approved"` on acceptance, otherwise the
        Owner's feedback text (used to drive the next regeneration pass).
        """
        ...


def lavish_available() -> bool:
    """True if lavish-axi can be invoked.

    lavish-axi is normally fetched on demand via `npx -y lavish-axi`, so the
    presence of `npx` is the primary signal; a directly-installed `lavish` or
    `lavish-axi` binary also counts.
    """
    return (
        shutil.which("lavish") is not None
        or shutil.which("lavish-axi") is not None
        or shutil.which("npx") is not None
    )


class LavishAdapter:
    """Review via lavish-axi, invoked through `npx -y lavish-axi`.

    Opens/resumes the review session, then long-polls for the Owner's feedback.
    When the Owner uses "Send & End" (or otherwise ends the session), the poll
    output carries a "session ended" marker — that means approval, since the
    Owner has signalled they are done.
    """

    BASE_CMD = ["npx", "-y", "lavish-axi"]
    # lavish-axi's own end-of-session signals in poll output.
    END_MARKERS = ("stop polling", "session ended", "session_ended")

    def review(self, artifact_path: str) -> str:
        # Open or resume the review session in the browser.
        subprocess.run(
            [*self.BASE_CMD, artifact_path],
            capture_output=True,
            text=True,
            check=False,
        )
        # Long-poll for feedback — blocks until the Owner acts.
        result = subprocess.run(
            [*self.BASE_CMD, "poll", artifact_path],
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        if self._is_ended(output):
            return APPROVED
        if output.lower() in ("", "approved", "ok", "yes"):
            return APPROVED
        return output

    @classmethod
    def _is_ended(cls, output: str) -> bool:
        lowered = output.lower()
        return any(marker in lowered for marker in cls.END_MARKERS)


class BasicAdapter:
    """Review via the OS browser + the `brigade up` terminal.

    Opens the artifact and blocks reading a line of feedback from stdin.
    """

    def review(self, artifact_path: str) -> str:
        self._open_browser(artifact_path)
        print(f"\n[designer] Review the concept at {artifact_path}")
        feedback = input("Feedback (empty or 'approved' to accept): ").strip()
        if feedback.lower() in ("", "approved", "ok", "yes"):
            return APPROVED
        return feedback

    @staticmethod
    def _open_browser(path: str) -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)


def select_review_adapter(
    review_tool: str = "auto", lavish_present: bool | None = None
) -> ReviewAdapter:
    """Choose an adapter from the config value.

    `review_tool` is one of "auto", "lavish", "basic".  "auto" probes for the
    lavish CLI at runtime and falls back to basic silently.
    """
    if lavish_present is None:
        lavish_present = lavish_available()

    if review_tool == "basic":
        return BasicAdapter()
    if review_tool == "lavish":
        return LavishAdapter()
    # "auto" (default) and any unknown value behave the same
    if lavish_present:
        return LavishAdapter()
    return BasicAdapter()
