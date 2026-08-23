# Patch — Interpreter Scope Classification

This is a patch, not a new phase — it assumes the core pipeline (`00`–`06`) is already implemented and working. It changes persona content and a small amount of MCP-server-adjacent logic, not the topology, message schema, or any worker's core behavior.

## The problem

Right now, every message to the Interpreter results in a full pipeline dispatch — `dispatch_behaviour` down through Analyst → Examiner → Builder and back — even for something like "how do I run this project," which needs no codebase change at all. This is wasteful (real cost and latency for a question that needed neither), and it also exposes a more important gap: nothing currently stops the Interpreter from doing the opposite — editing the project's code directly with its own file-write tools, bypassing the pipeline entirely. Both problems have the same fix: the Interpreter needs an explicit read/write classification step before it decides what to do with a message.

## The fix

### 1. Persona update (`AGENTS.md` / `CLAUDE.md`)

Add this classification step, to be applied before responding to any Owner message:

> Not every message enters the pipeline. Before responding:
> - If the request only needs reading or explaining what already exists (how something works, how to run it, why it behaves a certain way), answer directly using your own tools. Never call `dispatch_behaviour` for this.
> - If fulfilling the request means the codebase needs to change — even a one-line change — that must go through `dispatch_behaviour`. Never edit project code yourself, regardless of how small the change looks. The pipeline's guarantees only hold if every change passes through it.
> - If the request is asking for visual/creative direction rather than a concrete change, route to `dispatch_design_request` instead.
> - If a question turns out to reveal something that should change ("why doesn't X work" → "it doesn't, and it should"), answer the question first, then ask the Owner whether they want it fixed — only dispatch once that's confirmed, not automatically.

This replaces whatever currently-implicit "always dispatch" behavior exists in the persona content from Phase 4 — read through the existing `AGENTS.md`/`CLAUDE.md` output and remove or rewrite anything that implies every message should result in a dispatch.

### 2. Guardrail on the Interpreter's own tool access

The MCP server itself (from Phase 4) already exposes `dispatch_behaviour`, `dispatch_design_request`, `check_status`, `check_design_status`, and `log_conversation`. This patch adds no new tools — the fix here is entirely about restricting what the Interpreter does *outside* those tools:

- Confirm (or add, if not already true) that the Interpreter's harness session has no direct file-write access to the main project tree outside of what's needed to run read-only inspection commands. If the harness configuration currently grants unrestricted file-write tools to the Interpreter's own session, scope them down so writes are only possible inside worktrees the pipeline itself creates (`.brigade/work/`), never in the main tree directly.
- If scoping tool access that precisely isn't practical with your harness's permission model, the fallback is persona-only enforcement (the rule above) — note in the README which approach was actually used, since it changes how much you can trust the guarantee versus how much it depends on the model following instructions.

### 3. `question`/`result` logging for direct answers

When the Interpreter answers directly (no dispatch), it should still call `log_conversation(type: "question", ...)` / `log_conversation(type: "result", ...)` — these types already exist in the schema from Phase 1/4, this patch just makes sure they're actually used for the no-dispatch path, not only ever paired with a dispatch.

## Out of scope for this patch

- Any change to the Analyst/Examiner/Builder/Designer/Sentinel worker logic — none of them are touched by this.
- Any change to the message schema or topology — no new message types, no new edges.
- Automating the ambiguous "question that's really a bug report" case beyond asking the Owner — still a human decision, not something to infer automatically.

## Acceptance criteria

- [ ] Asking the Interpreter a genuinely read-only question ("how do I run this project," "what does this function do") results in a direct answer with no `dispatch_behaviour` call and no Analyst/Examiner/Builder activity in the logs.
- [ ] The direct-answer path still produces a `question`/`result` pair in the ledger, so the conversation stays fully audit-able even without a pipeline run.
- [ ] Asking for an actual code change, however small, still correctly routes through `dispatch_behaviour` — confirm this wasn't over-corrected into refusing legitimate small changes.
- [ ] Asking for visual/creative direction still correctly routes through `dispatch_design_request`, unaffected by this patch.
- [ ] The Interpreter does not edit any file inside the main project tree directly, in any test you can devise for this — including a deliberate attempt to get it to "just quickly fix" something itself. If tool-level scoping was implemented, verify the write attempt fails outright rather than being merely discouraged by the persona.
- [ ] A deliberately ambiguous message ("why doesn't the login button work?") results in an investigation and a direct answer first, with the Interpreter explicitly asking whether to dispatch a fix — not an automatic dispatch, and not just an answer with no follow-up offered.
