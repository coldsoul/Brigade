# Patch — Crash-Safe Message Processing

Assumes current `main` (through the worker-error-surfacing patch). This closes the last structural reliability hole: a worker that dies or fails *after* consuming a message but *before* delivering its reply loses that message with no way to recover it. Patch B made such a death visible; this makes the message recoverable.

## The bug

`consume()` (`src/brigade/storage/mailbox.py`) removes the inbox pointer as soon as the message is claimed, then returns it. The worker holds the message in memory while it calls the model and produces a reply. If anything fails in that window — the process is killed, the machine sleeps, a bug throws before `deliver()` — the pointer is already gone from the inbox and the reply was never delivered. The message is now referenced by nothing: not in an inbox, not delivered onward. A restart can't recover it, because there's no pointer left to find. The `behaviour-to-implement` ledger entry still exists, but nothing will ever act on it.

This defeats the core promise of the file-based mailbox design — that state on disk means a crash loses nothing. It currently holds for messages *sitting* in an inbox, but not for a message *being processed* when the crash happens.

## The fix

Introduce an `in-progress/` staging directory per role, parallel to `inbox/`. Claiming a message *moves* its pointer from `inbox/` to `in-progress/` (atomic rename) rather than deleting it. The pointer is only deleted once the reply has been successfully delivered. On startup, any pointer left in `in-progress/` is a message a previous run died on — it gets recovered back into the inbox.

### 1. `consume()` moves rather than deletes

`src/brigade/storage/mailbox.py`:

```python
def consume(role: str, message_id: str, brigade_dir: Path) -> Message:
    """Claim a message from *role*'s inbox for processing.

    Atomically moves the inbox pointer into `in-progress/` rather than deleting
    it, so a crash before the reply is delivered leaves a recoverable trace.
    The pointer is cleared only by `complete()`, after delivery succeeds.
    """
    inbox_pointer = brigade_dir / "mailboxes" / role / "inbox" / message_id
    if not inbox_pointer.exists():
        raise FileNotFoundError(f"No message '{message_id}' in {role}'s inbox")

    in_progress_dir = brigade_dir / "mailboxes" / role / "in-progress"
    in_progress_dir.mkdir(parents=True, exist_ok=True)
    in_progress_pointer = in_progress_dir / message_id
    os.replace(inbox_pointer, in_progress_pointer)  # atomic on same filesystem

    return read_message(message_id, brigade_dir)  # unchanged: read from ledger
```

`os.replace` is atomic within a filesystem, so there's no window where the pointer exists in both places or neither.

### 2. A new `complete()` that clears the in-progress pointer

```python
def complete(role: str, message_id: str, brigade_dir: Path) -> None:
    """Mark a claimed message as fully processed, removing its in-progress pointer.

    Called only after the worker has successfully delivered its reply (or
    deliberately produced no reply). Safe to call if the pointer is already
    gone.
    """
    in_progress_pointer = brigade_dir / "mailboxes" / role / "in-progress" / message_id
    in_progress_pointer.unlink(missing_ok=True)
```

### 3. A recovery step run at worker startup

```python
def recover_in_progress(role: str, brigade_dir: Path) -> list[str]:
    """Move any messages stranded in `in-progress/` back into the inbox.

    Called once when a worker starts. A pointer in `in-progress/` means a
    previous run claimed the message but never completed it (crash, kill, bug).
    Returns the list of recovered message ids so the caller can log them.
    """
    in_progress_dir = brigade_dir / "mailboxes" / role / "in-progress"
    if not in_progress_dir.is_dir():
        return []

    inbox_dir = brigade_dir / "mailboxes" / role / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    recovered = []
    for pointer in in_progress_dir.iterdir():
        os.replace(pointer, inbox_dir / pointer.name)
        recovered.append(pointer.name)
    return recovered
```

### 4. Wire it into the worker

`src/brigade/workers/base.py`:

- **At the start of `run()`**, before the poll loop, call `recover_in_progress(self.role, self.brigade_dir)` and log any recovered ids loudly (at WARNING, with `event: "message_recovered"`) — a recovered message means a prior unclean death, worth surfacing on the dashboard too.
- **In `run_once()`**, after `_dispatch(msg)` returns successfully, call `complete(self.role, msg_id, self.brigade_dir)`. If `_dispatch` raises, `complete` is **not** called — the pointer stays in `in-progress/` and will be recovered on the next startup, rather than being silently dropped. (This is a deliberate difference from Patch B's current behavior, where a per-message error is logged and skipped: now it's logged, surfaced, *and* the message survives for a retry on restart.)

Concretely, `run_once`'s inner loop becomes:

```python
for msg_id in ids:
    try:
        msg = consume(self.role, msg_id, self.brigade_dir)
        self._dispatch(msg)
        complete(self.role, msg_id, self.brigade_dir)   # only on success
    except Exception:
        self.logger.exception(
            "error processing message %s", msg_id,
            extra={"event": "worker_error", "message_id": msg_id},
        )
        # pointer stays in in-progress/ — recovered on next startup
```

### 5. One subtlety worth deciding: poison messages

A message that *always* fails (a genuinely malformed payload, or one that hits a deterministic bug) will now be recovered and retried forever across restarts — an infinite loop instead of a silent drop. That's arguably better (visible instead of silent), but worth bounding. Options, simplest first:

- **v1 (do this now):** just recover-and-retry. In practice most failures are transient (an API blip, a timeout), and a permanently-poison message is rare and now at least *loud* (it logs a `worker_error` every cycle). Ship this.
- **Later, if it becomes a problem:** track a retry count (e.g. a small sidecar file next to the in-progress pointer) and move a message to a `failed/` dir after N recoveries, so it stops looping and sits somewhere a human can inspect it. Note this as a follow-up; don't build it yet.

Pick v1 for this patch; leave the dead-letter idea as a documented future step.

## Out of scope for this patch

- Auto-restarting a dead worker (still Patch B's deferred item) — this patch ensures the *message* survives a worker death, but recovery only happens when a worker next starts. If a worker dies and never restarts, its in-progress message waits until the next `brigade up`. That's acceptable given `brigade up` is a foreground process you restart manually.
- The dead-letter/poison-message handling above (v1 recovers-and-retries only).
- Any change to `deliver()` — the delivery side is already atomic (ledger write + pointer drop) and isn't the gap here.

## Acceptance criteria

- [ ] `consume()` moves the pointer from `inbox/` to `in-progress/` (verify both: gone from inbox, present in in-progress) and does not delete it.
- [ ] `complete()` removes the in-progress pointer, and is idempotent (calling it twice, or on an already-gone pointer, doesn't raise).
- [ ] **The core crash-safety test:** simulate a worker dying mid-processing — `consume()` a message, then raise before `complete()` — and confirm the pointer is still in `in-progress/`, not lost. Then call `recover_in_progress()` and confirm the message is back in the inbox and processable.
- [ ] A message that processes successfully leaves nothing behind in `in-progress/`.
- [ ] `recover_in_progress()` at worker startup moves stranded messages back to the inbox and logs each at WARNING with `event: "message_recovered"`.
- [ ] A worker that fails on one message (per-message error) leaves that message in `in-progress/` for recovery, while still processing other pending messages in the same `run_once` — confirm the good messages complete and the bad one survives.
- [ ] End-to-end: dispatch a behaviour, kill `brigade up` while a worker is mid-processing (or simulate via the test above), restart `brigade up`, and confirm the behaviour resumes rather than being lost — the real-world version of the scenario that motivated this patch.
- [ ] Full existing test suite still passes (the `consume()` semantics changed, so any test asserting the old delete-on-consume behavior needs updating to the move-on-consume behavior).
