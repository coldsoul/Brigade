"""Designer worker — generates visual concepts in an isolated worktree.

The Designer reuses the Builder's "headless harness in its own worktree"
pattern, but instead of verified functional code it produces exploratory
HTML/CSS concepts, gated by a human review loop rather than a pass/fail verdict.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from brigade.harness import HarnessRunner
from brigade.messages import Message
from brigade.review import APPROVED, ReviewAdapter, select_review_adapter
from brigade.worktree import ensure_worktree, git_exclude
from brigade.workers.base import RoleWorker, WorkerError, build_reply

ARTIFACT_FILENAME = "concept.html"
SUMMARY_FILENAME = "design-summary.json"

CAP_NOTE = (
    "[ITERATION CAP REACHED — not approved. Ask the Owner whether to proceed "
    "with this concept as-is or abandon the design exploration.]"
)


class DesignerWorker(RoleWorker):
    role = "designer"

    def __init__(
        self,
        config,
        router,
        brigade_dir: Path,
        harness_runner=None,
        review_adapter=None,
        **kwargs,
    ):
        # The Designer never uses the router directly — the harness subprocess
        # manages its own model calls.
        super().__init__(config, router, brigade_dir, **kwargs)
        self.harness_runner = harness_runner or HarnessRunner()
        self.project_root = brigade_dir.parent
        self._injected_adapter = review_adapter
        self._resolved_adapter = None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def process(self, msg: Message) -> Message | None:
        if msg.type == "design-request":
            return self._handle_design_request(msg)
        return None

    # ------------------------------------------------------------------
    # Handler + review loop
    # ------------------------------------------------------------------

    def _handle_design_request(self, msg: Message) -> Message:
        behaviour_id = msg.behaviour_id
        text = msg.payload.get("text", "")
        worktree = ensure_worktree(self.project_root, f"{behaviour_id}-design")
        adapter = self._adapter()

        feedback = None
        iterations = 0
        approved = False
        max_loops = max(1, self.config.project.max_loops)
        artifact_path = worktree / ARTIFACT_FILENAME
        description = "a visual concept direction"

        while iterations < max_loops:
            artifact_path, description = self._generate(worktree, text, feedback, behaviour_id)
            iterations += 1
            result = adapter.review(str(artifact_path))
            if result == APPROVED:
                approved = True
                break
            feedback = result

        if not approved:
            description = f"{description}\n\n{CAP_NOTE}"

        payload = {
            "artifact_ref": str(artifact_path.relative_to(self.project_root)),
            "description": description,
            "iterations": iterations,
        }
        return build_reply(msg, "interpreter", "design-result", payload)

    # ------------------------------------------------------------------
    # Harness invocation
    # ------------------------------------------------------------------

    def _generate(
        self, worktree: Path, text: str, feedback: str | None, behaviour_id: str
    ) -> tuple[Path, str]:
        harness = self._designer_harness()
        model = self._designer_model()

        git_exclude(worktree, ARTIFACT_FILENAME)
        git_exclude(worktree, SUMMARY_FILENAME)
        artifact_path = worktree / ARTIFACT_FILENAME
        summary_path = worktree / SUMMARY_FILENAME

        json_example = '{"description": "<plain-language summary of the direction>"}'

        if feedback:
            # Revise the existing document in place — do NOT delete it, the
            # harness reads the current draft to apply the feedback.
            instruction = (
                "Revise the existing HTML document in place to address the "
                f"following review feedback for this need:\n\n{text}\n\n"
                f"REVIEW FEEDBACK TO ADDRESS:\n{feedback}\n\n"
                f"Read the current HTML document at {artifact_path}, revise it "
                f"accordingly, and write the updated document back to this "
                f"exact path:\n{artifact_path}\n\n"
                f"Write a single JSON object {json_example} to this exact path:\n"
                f"{summary_path}"
            )
        else:
            instruction = (
                "Generate a runnable HTML/CSS concept direction for the "
                f"following need:\n\n{text}\n\n"
                f"Write the HTML document to this exact path:\n{artifact_path}\n\n"
                f"Write a single JSON object {json_example} to this exact path:\n"
                f"{summary_path}"
            )

        prompt = self._build_prompt(instruction)

        start = time.monotonic()
        result = self.harness_runner.run(harness, model, worktree, prompt)
        duration = time.monotonic() - start

        self._log_harness(behaviour_id, result, duration)

        description = self._read_description(summary_path)
        return artifact_path, description

    def _read_description(self, summary_path: Path) -> str:
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            return str(data.get("description", ""))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return "a visual concept direction"

    # ------------------------------------------------------------------
    # Adapter + config helpers
    # ------------------------------------------------------------------

    def _adapter(self) -> ReviewAdapter:
        if self._injected_adapter is not None:
            return self._injected_adapter
        if self._resolved_adapter is None:
            self._resolved_adapter = select_review_adapter(self._review_tool())
        return self._resolved_adapter

    def _review_tool(self) -> str:
        designer = self.config.roles.get("designer")
        if designer is None:
            return "auto"
        return designer.review_tool or "auto"

    def _designer_harness(self) -> str:
        designer = self.config.roles.get("designer")
        if designer is None or designer.harness is None:
            raise WorkerError(
                "[roles.designer].harness is not configured — set it to "
                "'claude' or 'opencode' in config.toml"
            )
        return designer.harness

    def _designer_model(self) -> str:
        designer = self.config.roles.get("designer")
        if designer is None or designer.model is None:
            raise WorkerError("[roles.designer].model is not configured")
        return designer.model

    def _build_prompt(self, instruction: str) -> str:
        return f"{self.persona}\n\n{instruction}"
