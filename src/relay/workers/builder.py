"""Builder worker — spawns headless harness sessions in isolated worktrees.

Unlike Analyst/Examiner, the Builder is not a single LLM call: it creates a git
worktree per behaviour, invokes a headless coding harness inside it, and reads
back the evidence report the harness wrote before packaging it into an
`evidence` message.
"""

from __future__ import annotations

import json
from pathlib import Path

from relay.harness import HarnessRunner
from relay.messages import EvidencePayload, Message
from relay.worktree import ensure_worktree
from relay.workers.base import RoleWorker, WorkerError, build_reply

# Where the harness writes its evidence report, and where the wrapper reads it.
EVIDENCE_DIR_NAME = "evidence"


class BuilderWorker(RoleWorker):
    role = "builder"

    def __init__(self, config, router, relay_dir: Path, harness_runner=None, **kwargs):
        # The Builder never uses the router directly — the harness subprocess
        # manages its own model calls.
        super().__init__(config, router, relay_dir, **kwargs)
        self.harness_runner = harness_runner or HarnessRunner()
        self.project_root = relay_dir.parent

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
        worktree = ensure_worktree(self.project_root, msg.behaviour_id)

        prompt = self._build_prompt(
            "Implement the following expectations and prove each one by actually "
            "running something.\n\n"
            f"EXPECTATIONS:\n{json.dumps(expectations, indent=2)}\n\n"
            f"INTEGRATION EXPECTATION:\n{integration}"
        )
        evidence = self._run_harness(worktree, msg.behaviour_id, prompt, expectations)
        return build_reply(msg, "examiner", "evidence", evidence)

    def _handle_verdict(self, msg: Message) -> Message:
        unmet = msg.payload.get("unmet", [])
        worktree = ensure_worktree(self.project_root, msg.behaviour_id)

        prompt = self._build_prompt(
            "The following expectations were not met. Resume work in this same "
            "worktree and address each unmet expectation, then prove your fixes "
            "by running something.\n\n"
            f"UNMET EXPECTATIONS:\n{json.dumps(unmet, indent=2)}"
        )
        evidence = self._run_harness(worktree, msg.behaviour_id, prompt, unmet)
        return build_reply(msg, "examiner", "evidence", evidence)

    # ------------------------------------------------------------------
    # Harness invocation + evidence packaging
    # ------------------------------------------------------------------

    def _run_harness(
        self, worktree: Path, behaviour_id: str, prompt: str, expectations: list
    ) -> dict:
        harness = self._builder_harness()
        model = self._builder_model()
        evidence_path = (
            self.relay_dir / EVIDENCE_DIR_NAME / f"{behaviour_id}.json"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)

        full_prompt = (
            f"{prompt}\n\n"
            "As your final action, write your evidence report as a single JSON "
            f"object to this exact path:\n{evidence_path}"
        )

        result = self.harness_runner.run(harness, model, worktree, full_prompt)

        # Log the full harness output for debugging (never goes into the ledger).
        print(
            f"[{self.role}] harness exited {result.exit_code} "
            f"({' '.join(result.command)})"
        )
        if result.stderr:
            print(f"[{self.role}] harness stderr: {result.stderr.strip()[:2000]}")

        return self._read_evidence(evidence_path, result, expectations)

    def _read_evidence(
        self, evidence_path: Path, result, expectations: list
    ) -> dict:
        try:
            data = json.loads(evidence_path.read_text(encoding="utf-8"))
            validated = EvidencePayload.model_validate(data)
            return validated.model_dump()
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            # The harness failed to produce a valid evidence report.  Produce a
            # conservative narrative fallback so the Examiner has something to
            # judge rather than the worker silently dropping the round.
            return self._fallback_evidence(result, expectations, exc)

    def _fallback_evidence(self, result, expectations: list, exc: Exception) -> dict:
        raw = (result.stderr or result.stdout or "").strip()[:1000]
        return {
            "evidence": [
                {
                    "expectation_id": exp["id"],
                    "claim": "the harness session did not produce valid evidence",
                    "execution": {
                        "command": " ".join(result.command),
                        "raw_output": raw,
                        "artifact_ref": None,
                    },
                    "confidence": "narrative",
                }
                for exp in expectations
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
