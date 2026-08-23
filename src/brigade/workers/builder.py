"""Builder worker — spawns headless harness sessions in isolated worktrees.

Unlike Analyst/Examiner, the Builder is not a single LLM call: it creates a git
worktree per behaviour, invokes a headless coding harness inside it, and reads
back the evidence report the harness wrote before packaging it into an
`evidence` message.
"""

from __future__ import annotations

import json
from pathlib import Path

from brigade.harness import HarnessRunner
from brigade.messages import EvidencePayload, Message
from brigade.worktree import ensure_worktree, git_exclude
from brigade.workers.base import RoleWorker, WorkerError, build_reply

# The evidence report is written *inside* the worktree (so the harness can
# write to it — harnesses restrict writes to their workspace) and git-excluded
# so it never leaks into the committed diff.
EVIDENCE_FILENAME = "brigade-evidence.json"


class BuilderWorker(RoleWorker):
    role = "builder"

    def __init__(self, config, router, brigade_dir: Path, harness_runner=None, **kwargs):
        # The Builder never uses the router directly — the harness subprocess
        # manages its own model calls.
        super().__init__(config, router, brigade_dir, **kwargs)
        self.harness_runner = harness_runner or HarnessRunner()
        self.project_root = brigade_dir.parent

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def process(self, msg: Message) -> Message | None:
        if msg.type == "expectation":
            return self._handle_expectation(msg)
        if msg.type == "verdict":
            return self._handle_verdict(msg)
        return None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_expectation(self, msg: Message) -> Message:
        expectations = msg.payload.get("expectations", [])
        integration = msg.payload.get("integration_expectation", "")
        expectation_ids = [e.get("id", "") for e in expectations]
        worktree = ensure_worktree(self.project_root, msg.behaviour_id)

        prompt = self._build_prompt(
            "Implement the following expectations and prove each one by actually "
            "running something.\n\n"
            f"EXPECTATIONS:\n{json.dumps(expectations, indent=2)}\n\n"
            f"INTEGRATION EXPECTATION:\n{integration}"
        )
        evidence = self._run_harness(worktree, prompt, expectation_ids)
        return build_reply(msg, "examiner", "evidence", evidence)

    def _handle_verdict(self, msg: Message) -> Message:
        unmet = msg.payload.get("unmet", [])
        expectation_ids = [u.get("expectation_id", "") for u in unmet]
        worktree = ensure_worktree(self.project_root, msg.behaviour_id)

        prompt = self._build_prompt(
            "The following expectations were not met. Resume work in this same "
            "worktree and address each unmet expectation, then prove your fixes "
            "by running something.\n\n"
            f"UNMET EXPECTATIONS:\n{json.dumps(unmet, indent=2)}"
        )
        evidence = self._run_harness(worktree, prompt, expectation_ids)
        return build_reply(msg, "examiner", "evidence", evidence)

    # ------------------------------------------------------------------
    # Harness invocation + evidence packaging
    # ------------------------------------------------------------------

    def _run_harness(
        self, worktree: Path, prompt: str, expectation_ids: list[str]
    ) -> dict:
        harness = self._builder_harness()
        model = self._builder_model()

        # The evidence file lives inside the worktree so the harness can write
        # to it.  It is git-excluded so it never ends up in the merged diff.
        git_exclude(worktree, EVIDENCE_FILENAME)
        evidence_path = worktree / EVIDENCE_FILENAME
        # Remove any stale report from a previous round so a failed run can't
        # silently read old evidence.
        evidence_path.unlink(missing_ok=True)

        full_prompt = (
            f"{prompt}\n\n"
            "As your final action, write your evidence report as a single JSON "
            f"object to this exact path:\n{evidence_path}"
        )

        result = self.harness_runner.run(harness, model, worktree, full_prompt)

        # Log the full harness output for debugging (never goes into the ledger).
        self.logger.debug("harness exited %s", result.exit_code)
        if result.stdout:
            self.logger.debug("harness stdout:\n%s", result.stdout.strip()[:2000])
        if result.stderr:
            self.logger.debug("harness stderr:\n%s", result.stderr.strip()[:2000])

        return self._read_evidence(evidence_path, result, expectation_ids)

    def _read_evidence(
        self, evidence_path: Path, result, expectation_ids: list[str]
    ) -> dict:
        try:
            data = json.loads(evidence_path.read_text(encoding="utf-8"))
            validated = EvidencePayload.model_validate(data)
            return validated.model_dump()
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            # The harness failed to produce a valid evidence report.  Produce a
            # conservative narrative fallback so the Examiner has something to
            # judge rather than the worker silently dropping the round.
            self.logger.warning("no valid evidence written: %s", exc)
            return self._fallback_evidence(result, expectation_ids)

    def _fallback_evidence(
        self, result, expectation_ids: list[str]
    ) -> dict:
        raw = (result.stderr or result.stdout or "").strip()[:1000]
        return {
            "evidence": [
                {
                    "expectation_id": expectation_id,
                    "claim": "the harness session did not produce valid evidence",
                    "execution": {
                        "command": " ".join(result.command),
                        "raw_output": raw,
                        "artifact_ref": None,
                    },
                    "confidence": "narrative",
                }
                for expectation_id in expectation_ids
            ],
            "test_files_touched": [],
        }

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _builder_harness(self) -> str:
        builder = self.config.roles.get("builder")
        if builder is None or builder.harness is None:
            raise WorkerError(
                "[roles.builder].harness is not configured — set it to "
                "'claude' or 'opencode' in config.toml"
            )
        return builder.harness

    def _builder_model(self) -> str:
        builder = self.config.roles.get("builder")
        if builder is None or builder.model is None:
            raise WorkerError(
                "[roles.builder].model is not configured"
            )
        return builder.model

    def _build_prompt(self, instruction: str) -> str:
        return f"{self.persona}\n\n{instruction}"
