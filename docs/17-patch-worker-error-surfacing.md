# Patch B — Surface Worker Errors and Liveness

Assumes current `main` plus Patch A. Patch A catches everything checkable *before* launch. This patch is the safety net for everything that can't be: a provider outage, a genuinely hung call, an auth rejection mid-run, a bug in a worker — anything that makes a worker fail or die *after* startup. Today those failures are invisible: the worker's thread dies or blocks, `brigade.log` shows nothing after the last `consuming` line, and the dashboard renders that stale state forever. This is the structural hole that made the original 30-minute hunt possible; Patch A fixed the specific trigger, this fixes the class.

## The bug

Workers run as `daemon=True` threads with no top-level error handling that reaches a visible surface, and no liveness tracking. Three consequences:
1. An exception at the top of a worker's run loop ends the thread; its traceback goes to stderr — which the TUI fd-redirect sends into `stray-output.log`, a file no one thinks to check — or nowhere useful.
2. The main (TUI) thread never learns a worker died; it keeps showing the worker's last-known state indefinitely.
3. There's no way, from the dashboard, to tell "working" from "dead" from "hung."

## The fix

Three parts: catch-and-log at the top of every worker loop, emit a visible error event, and track liveness on the dashboard.

### 1. Top-level catch-and-log in the worker run loop

In the base worker's `run()` (`workers/base.py`), wrap the per-message processing body so **no exception can escape the loop silently**. An error processing one message should be logged, surfaced, and — depending on kind — either skip that message or stop the worker cleanly, but never vanish:

```python
def run(self) -> None:
    self.logger.info("worker started", extra={"event": "worker_start"})
    try:
        while not self._stop.is_set():
            msg_id = self._next_message()  # existing inbox poll
            if msg_id is None:
                time.sleep(self.poll_interval)
                continue
            try:
                self._process(msg_id)      # existing consume→call→deliver
            except Exception:
                self.logger.exception(
                    "error processing message %s", msg_id,
                    extra={"event": "worker_error", "message_id": msg_id},
                )
                # do NOT re-raise — one bad message must not kill the worker
    except Exception:
        # anything outside per-message handling (e.g. the poll itself failing)
        self.logger.exception("worker loop crashed", extra={"event": "worker_crashed"})
    finally:
        self.logger.error("worker exiting", extra={"event": "worker_exit"})
```

Key decisions:
- `self.logger.exception(...)` logs the **full traceback to the role's own logger** → it lands in `brigade.log`, the file people actually read, not `stray-output.log`.
- A per-message error is caught and the loop **continues** — one malformed message or one transient API error must not take the whole worker down. (Whether a specific error should instead halt the worker is a judgment call; default to continue, and only halt for errors that clearly mean the worker can't function at all.)
- The `finally` guarantees that a worker stopping — for *any* reason, including a clean stop — logs `worker_exit` at ERROR level. A worker thread ending is always a loud, logged event now, never silent.

### 2. Emit error events the TUI can render

`worker_error` / `worker_crashed` / `worker_exit` are already structured log events (via the `extra=` above), so the existing `TUILogHandler` (`tui/bridge.py`) will convert them into `RoleEvent`s automatically — no new event type needed, just handle these `event` values in the Overview screen. On a `worker_error`, mark the role's status cell with a distinct error style (e.g. red `"ERROR — <short message>"`); on `worker_exit`/`worker_crashed`, mark the role as stopped/dead. These must be visually unmistakable — the whole point is that a failed worker *screams* rather than blends into a stale-looking-but-fine row.

### 3. Liveness tracking on the dashboard

Even with (1) and (2), a worker *blocked* (not crashed) — e.g. a hung call that Patch's timeout somehow doesn't cover, or a deadlock — wouldn't emit any event, so it'd still look frozen. Add a cheap liveness check the main thread runs on its existing render tick:

- The `up` command already holds the list of worker `Thread` objects. Pass that handle (or a small registry) to the `BrigadeApp`.
- On each periodic refresh, check `thread.is_alive()` per worker. If a worker's thread is **not alive** but the app never received a `worker_exit` for it, that's an unclean death — render it as `DIED (no exit logged)`, which is strictly worse than a logged exit and worth distinguishing.
- Optionally, track "time since last event" per role and visually flag a role that's been in a non-idle state (e.g. `consuming`) with no follow-up for longer than its configured `model_timeout_seconds` + a margin — this catches the "blocked, not dead" case that liveness-of-thread alone misses. This is the dashboard-side complement to the timeout patch: the timeout makes the call fail; this makes an *unexpectedly* long silence visible even if it doesn't.

## Interaction with the fd-redirect

Once (1) routes worker tracebacks to `brigade.log` via the role logger, the fd-redirect's `stray-output.log` should go back to being genuinely empty in normal operation — a non-empty `stray-output.log` becomes a meaningful signal again ("something bypassed structured logging entirely"), rather than the place important tracebacks were hiding. Worth noting in the README so it's understood as a diagnostic, not dead weight.

## Out of scope for this patch

- Automatically *restarting* a dead worker — surfacing the death clearly is this patch's job; auto-restart is a separate, more involved reliability feature (needs care around the in-flight message the worker died on — see the mailbox crash-safety concern, which is its own future patch).
- The mailbox consume-then-deliver crash-safety gap (a worker dying mid-`_process` after consuming can still lose that message). That's real and related but distinct — worth its own patch. This patch makes such a death *visible*; it doesn't yet make the message *recoverable*.
- Config-time validation (Patch A).

## Acceptance criteria

- [ ] An exception deliberately raised inside `_process` for one message is logged with a full traceback to `brigade.log` (via the role logger), and the worker continues processing subsequent messages rather than dying — verify both the log entry and the continued operation.
- [ ] The same error surfaces on the Overview screen as a distinct, unmistakable error state on that role's row, not a silently stale status.
- [ ] A worker thread that exits for any reason logs `worker_exit` at ERROR level — confirm the `finally` fires on both clean stop and exception paths.
- [ ] A worker whose thread is killed/dies without logging `worker_exit` is detected by the liveness check and rendered as `DIED` on the dashboard within one refresh tick.
- [ ] A role stuck in a non-idle state well past its `model_timeout_seconds` is visually flagged as potentially stuck (if the optional silence-detection is implemented).
- [ ] After this patch, reproducing the original scenario (a misconfigured/failing model call) shows the error on the dashboard and in `brigade.log` immediately — not a silent frozen row. (Note: Patch A prevents *this specific* misconfiguration from launching at all; verify Patch B with a runtime failure that Patch A can't catch, e.g. a present-but-invalid API key that fails at call time.)
- [ ] `stray-output.log` is empty in a normal run with a deliberate worker error — confirming the traceback went to `brigade.log` instead.
- [ ] Full existing test suite still passes.
