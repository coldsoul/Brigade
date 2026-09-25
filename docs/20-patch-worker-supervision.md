# Patch — Worker Supervision and Auto-Restart

Assumes current `main` plus the crash-safe processing patch (18) — the two are complementary and 18 should land first (see "Why ordering matters" below). This builds on Patch B's liveness *detection* by adding liveness *recovery*: a worker whose thread dies is automatically restarted, and the TUI shows the death and restart clearly rather than just marking the role dead forever.

## Why this needs 18 first

Restarting a worker is only *safe* if the message it died on isn't lost. Patch 18's `recover_in_progress()` (run at worker startup) is exactly what makes a restart pick up where the dead worker left off — the stranded in-progress message moves back to the inbox and the fresh thread processes it. Without 18, an auto-restarted worker would come back up but the message that killed it would already be gone. So: land 18 first, then this patch's restart becomes genuine recovery rather than "come back up and forget."

## The design shift this forces

`up` currently builds `threading.Thread` objects inline and hands the raw thread list to the app. A supervisor can't reuse a dead `Thread` — a stopped thread object can't be `.start()`ed again — so it needs to *recreate* the thread from the worker object. That means the supervisor must hold the worker objects (which are reusable), not just their threads.

So the ownership changes: instead of `up` creating threads and the app checking `is_alive()`, a small `WorkerSupervisor` owns the workers, spawns their threads, monitors them, and respawns dead ones. Both `up` and the TUI talk to the supervisor.

## The fix

### 1. A `WorkerSupervisor`

New module, e.g. `src/brigade/supervisor.py`:

```python
import threading
import time
import logging

class WorkerSupervisor:
    """Owns the role workers, runs each in a thread, and restarts dead ones.

    A worker thread ending is treated as failure (workers are meant to run
    forever), so the supervisor respawns it — with backoff, so a worker that
    dies immediately and repeatedly doesn't spin.
    """

    def __init__(self, workers: list, restart_backoff: float = 2.0,
                 max_backoff: float = 60.0):
        # workers: objects with a .role attribute and a .run() method
        self._workers = {w.role: w for w in workers}
        self._threads: dict[str, threading.Thread] = {}
        self._restarts: dict[str, int] = {r: 0 for r in self._workers}
        self._next_allowed: dict[str, float] = {r: 0.0 for r in self._workers}
        self._restart_backoff = restart_backoff
        self._max_backoff = max_backoff
        self._stop = threading.Event()

    def start(self) -> None:
        for role in self._workers:
            self._spawn(role)

    def _spawn(self, role: str) -> None:
        t = threading.Thread(target=self._workers[role].run, daemon=True, name=role)
        self._threads[role] = t
        t.start()

    def check_and_restart(self) -> list[str]:
        """Respawn any dead worker whose backoff has elapsed. Returns restarted roles.

        Called on a timer (by the TUI's existing render tick, or a dedicated
        supervisor thread). Backoff means a crash-looping worker is retried at
        widening intervals rather than hammered.
        """
        if self._stop.is_set():
            return []
        now = time.monotonic()
        restarted = []
        for role, thread in self._threads.items():
            if thread.is_alive():
                continue
            if now < self._next_allowed[role]:
                continue  # still in backoff
            self._restarts[role] += 1
            delay = min(self._restart_backoff * (2 ** (self._restarts[role] - 1)),
                        self._max_backoff)
            self._next_allowed[role] = now + delay
            logging.getLogger(role).error(
                "worker thread died — restarting (restart #%d, next backoff %.0fs)",
                self._restarts[role], delay,
                extra={"event": "worker_restart", "restart_count": self._restarts[role]},
            )
            self._spawn(role)
            restarted.append(role)
        return restarted

    def status(self) -> dict[str, dict]:
        """Per-role liveness snapshot for the TUI."""
        return {
            role: {
                "alive": self._threads[role].is_alive(),
                "restarts": self._restarts[role],
            }
            for role in self._workers
        }

    def stop(self) -> None:
        self._stop.set()
```

Design decisions baked in:
- **A worker thread ending is always failure.** Workers run forever by contract; a clean exit and a crash are both "it stopped running and shouldn't have," so both trigger a restart. (Patch B's `worker_exit`/`worker_crashed` logging still happens from inside the worker; this is the supervisor reacting to the thread being gone, regardless of how it went.)
- **Exponential backoff, capped.** A worker that dies instantly on startup (e.g. a persistent bug in `run()` itself, outside the per-message try/except) would otherwise respawn in a tight loop. Backoff widens the retry interval (2s, 4s, 8s… capped at 60s) so a genuinely broken worker fails slowly and visibly instead of spinning the CPU.
- **Restart count is tracked and surfaced.** A worker on its 5th restart is a different situation from a healthy one — the count is data the TUI should show, because repeated restarts mean something is actually wrong (and 18's recovered message may be a poison message re-killing the worker each time — see cross-reference below).

### 2. `up` hands workers to the supervisor, not raw threads

`src/brigade/cli.py`, replacing the current inline thread creation:

```python
from brigade.supervisor import WorkerSupervisor

workers = [
    AnalystWorker(config, router, brigade_dir),
    ExaminerWorker(config, router, brigade_dir),
    BuilderWorker(config, router, brigade_dir),
    DesignerWorker(config, router, brigade_dir),
    Sentinel(config, router, brigade_dir),   # Sentinel has .role and .run() too — treat uniformly
]

supervisor = WorkerSupervisor(workers)
supervisor.start()

app = BrigadeApp(brigade_dir, supervisor=supervisor)
```

Confirm `Sentinel` exposes a `.role` attribute (`"sentinel"`) and a `.run()` — if it doesn't already, give it one so the supervisor can treat all five uniformly rather than special-casing it.

### 3. TUI consumes the supervisor instead of a raw thread list

`src/brigade/tui/app.py`:
- `BrigadeApp.__init__` takes `supervisor` instead of `worker_threads`.
- The existing `_check_liveness` timer becomes the supervisor's driver: call `supervisor.check_and_restart()` on the tick, then reflect `supervisor.status()` in the role table.
- **Rendering states** — each role's status cell reflects one of:
  - alive & working → existing behavior (consuming / producing / idle)
  - just restarted → a brief, distinct `↻ RESTARTED (#n)` flash, then back to normal once it logs activity
  - dead & in backoff → `✖ DOWN — restarting in Ns (restart #n)`, so a crash-looping worker is unmistakable and you can see it's being retried, not abandoned
  - restart count climbing → show the count persistently once it's above 0, e.g. a small `↻n` badge on the role row, so even a currently-alive-but-flaky worker reveals its history

The `worker_restart` event the supervisor logs also flows through `TUILogHandler` as a `RoleEvent` (same as the other structured events), so the ledger/log has a record of every restart, not just the live view.

### 4. Cross-reference the poison-message interaction (from patch 18)

There's a real failure mode where these two patches combine badly: a poison message (one that deterministically kills the worker during `_dispatch`) gets recovered by 18 on restart, re-kills the fresh worker, gets recovered again — a restart loop. The backoff here contains the *spin* (it won't hammer), and the climbing restart count makes it *visible*, but it doesn't *resolve* it. This is the concrete case that motivates patch 18's deferred dead-letter (`failed/` dir after N recoveries) step. Note in the code/README that if you see a worker's restart count climbing steadily with a recovered message each cycle, that's a poison message and the dead-letter follow-up is the fix. Don't build the dead-letter here — just make sure the restart-count surfacing makes the situation diagnosable rather than mysterious.

## Out of scope for this patch

- The dead-letter/poison-message handling itself (patch 18's deferred step) — this patch makes the poison-loop *visible and non-spinning*, not resolved.
- Restarting the whole process, or the MCP server / Interpreter side — this is only about the five brigade worker threads inside `brigade up`.
- Configurable backoff parameters — hardcode sensible defaults (2s base, 60s cap) for now; make them config-driven only if a real need appears.

## Acceptance criteria

- [ ] A worker whose thread dies (simulate: a worker whose `run()` raises immediately) is restarted by `check_and_restart()`, and `status()` reflects the incremented restart count.
- [ ] Restart backoff widens on repeated deaths (verify the `_next_allowed` interval grows) and is capped at `max_backoff` — a worker that always dies does not respawn faster than the cap.
- [ ] A healthy worker is never restarted — `check_and_restart()` on an all-alive set returns an empty list and touches nothing.
- [ ] Each restart logs a `worker_restart` event (with `restart_count`) via the role logger, so it lands in `brigade.log` and flows to the TUI.
- [ ] The TUI shows a dead-and-restarting worker distinctly from a healthy one, including the backoff countdown and restart count — verify by eye against a worker rigged to die once.
- [ ] With patch 18 in place, a worker killed mid-processing is restarted AND resumes the recovered message — the end-to-end recovery path works, not just the respawn.
- [ ] A poison message (deterministically kills the worker) produces a visibly climbing restart count with backoff, not a tight spin or a silent stall — confirm the situation is diagnosable from the dashboard.
- [ ] `Ctrl+C`/`q` stops the supervisor (no respawn after stop is signalled) and exits cleanly with no orphaned threads.
- [ ] Full existing test suite still passes — the `up` handoff changed from `worker_threads` to `supervisor`, so Patch B's liveness tests need updating to the supervisor-backed path.
