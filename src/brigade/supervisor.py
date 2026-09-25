"""Worker supervision — liveness recovery for the brigade role workers.

A `WorkerSupervisor` owns the role worker objects, runs each in a daemon
thread, and restarts any thread that ends.  Workers are meant to run forever,
so a thread ending is always treated as failure and respawned with widening
backoff so a crash-looping worker fails slowly and visibly rather than
spinning the CPU.
"""

from __future__ import annotations

import logging
import threading
import time


class WorkerSupervisor:
    """Owns the role workers, runs each in a thread, and restarts dead ones."""

    def __init__(
        self,
        workers: list,
        restart_backoff: float = 2.0,
        max_backoff: float = 60.0,
    ):
        # workers: objects with a .role attribute and a .run() method
        self._workers = {w.role: w for w in workers}
        self._threads: dict[str, threading.Thread] = {}
        self._restarts: dict[str, int] = {r: 0 for r in self._workers}
        self._next_allowed: dict[str, float] = {r: 0.0 for r in self._workers}
        self._restart_backoff = restart_backoff
        self._max_backoff = max_backoff
        self._stop = threading.Event()

    def start(self) -> None:
        """Spawn one thread per worker."""
        for role in self._workers:
            self._spawn(role)

    def _spawn(self, role: str) -> None:
        t = threading.Thread(target=self._workers[role].run, daemon=True, name=role)
        self._threads[role] = t
        t.start()

    def check_and_restart(self) -> list[str]:
        """Respawn any dead worker whose backoff has elapsed.

        Returns the list of roles restarted.  Called on a timer (by the TUI's
        render tick).  Backoff means a crash-looping worker is retried at
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
            delay = min(
                self._restart_backoff * (2 ** (self._restarts[role] - 1)),
                self._max_backoff,
            )
            self._next_allowed[role] = now + delay
            logging.getLogger(role).error(
                "worker thread died — restarting (restart #%d, next backoff %.0fs)",
                self._restarts[role],
                delay,
                extra={
                    "event": "worker_restart",
                    "restart_count": self._restarts[role],
                },
            )
            self._spawn(role)
            restarted.append(role)
        return restarted

    def status(self) -> dict[str, dict]:
        """Per-role liveness snapshot for the TUI."""
        now = time.monotonic()
        return {
            role: {
                "alive": self._threads[role].is_alive()
                if role in self._threads
                else False,
                "restarts": self._restarts[role],
                "retry_in": max(0.0, self._next_allowed[role] - now),
            }
            for role in self._workers
        }

    def stop(self) -> None:
        """Signal the supervisor to stop respawning dead workers."""
        self._stop.set()
