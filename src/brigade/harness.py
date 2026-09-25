"""Headless coding-harness invocation.

Runs the configured harness (`claude` or `opencode`) as a subprocess inside a
behaviour's worktree.  The runner is injectable so the Builder can be tested
with a fake harness without spawning a real coding session.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HarnessResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str


def harness_model_name(harness: str, model: str) -> str:
    """Return the model string to pass to the harness CLI.

    The config stores `provider/model` (LiteLLM-style).  `claude` expects just
    the model name, so the provider prefix is stripped; `opencode` accepts the
    full `provider/model` form.
    """
    if harness == "claude":
        return model.split("/", 1)[-1]
    return model


class HarnessRunner:
    """Runs a headless harness subprocess and captures its full output."""

    def run(
        self, harness: str, model: str, workdir: Path, prompt: str
    ) -> HarnessResult:
        command = self._build_command(harness, model, prompt)
        proc = subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        return HarnessResult(
            command=command,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )

    def _build_command(self, harness: str, model: str, prompt: str) -> list[str]:
        if harness == "claude":
            return [
                "claude",
                "-p",
                prompt,
                "--model",
                harness_model_name(harness, model),
            ]
        if harness == "opencode":
            return [
                "opencode",
                "run",
                "--auto",
                "--pure",
                prompt,
                "--model",
                harness_model_name(harness, model),
            ]
        raise ValueError(
            f"Unknown harness: {harness!r} — expected 'claude' or 'opencode'"
        )
