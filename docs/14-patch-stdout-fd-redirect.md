# Patch — Stop Third-Party Output From Drawing Over the TUI

Assumes phase 12 (`902f99b`) is implemented. This is a bug fix — `configure_logging(console=False)` plus `silence_litellm()` were a reasonable first attempt, but they only cover Python's `logging` module and only the specific logger names known about today. Anything that writes to the terminal by another path — a raw `print()`, a library that attaches its own handler directly, a new logger name introduced by a dependency update — still corrupts the dashboard.

## Why the current approach is the wrong shape, not just incomplete

`silence_litellm()` (`src/brigade/logging_config.py`) is an allow-list of logger names (`"LiteLLM"`, `"LiteLLM Router"`, `"LiteLLM Proxy"`, `"litellm"`). This has two structural problems:

1. It only silences things that go through Python's `logging` module. Any dependency that uses `print()` directly, or writes at a lower level (some HTTP client debug output, C-extension writes), is untouched regardless of logger names.
2. It has to be kept in sync with every dependency's internal logging setup, across every version — litellm wraps httpx and various provider SDKs, and their logger names and behavior aren't under this project's control and can change without warning.

The fix is structural: while the TUI owns the terminal, redirect the actual OS-level file descriptors (`1`/`2`), not just Python's logging layer. This catches everything, from every source, permanently — no name list to maintain.

## The fix

### 1. A context manager that redirects real stdout/stderr to the log file

New function in `src/brigade/logging_config.py`:

```python
import contextlib
import os
import sys


@contextlib.contextmanager
def redirect_fds_to_file(path: Path):
    """Redirect the process's real stdout/stderr file descriptors to *path*.

    Unlike `contextlib.redirect_stdout`, this operates at the OS file-descriptor
    level (via `os.dup2`), so it catches writes from C extensions and any
    dependency that writes directly to fd 1/2 rather than through Python's
    `sys.stdout`/`logging` — which is exactly what's needed while a Textual app
    owns the terminal. Restores the original fds on exit, including on an
    unhandled exception.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)

    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()
    saved_stdout_fd = os.dup(stdout_fd)
    saved_stderr_fd = os.dup(stderr_fd)

    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log_fd, stdout_fd)
        os.dup2(log_fd, stderr_fd)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout_fd, stdout_fd)
        os.dup2(saved_stderr_fd, stderr_fd)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        os.close(log_fd)
```

### 2. Wrap the TUI's `app.run()` call with it

`src/brigade/cli.py`, in the `up` command, where `app.run()` is currently called directly:

```python
from brigade.logging_config import redirect_fds_to_file

stray_output_path = brigade_dir / "logs" / "stray-output.log"
with redirect_fds_to_file(stray_output_path):
    app.run()
```

Use a separate file (`stray-output.log`) rather than `brigade.log` itself — this file is a catch-all for whatever wasn't going through the structured logging path in the first place, and mixing it into the main log would undo the readability work from the earlier logging phases. It's fine (expected, even) for this file to be empty most of the time — it existing and being non-empty after a run is itself a useful signal that something new is bypassing structured logging and may be worth a follow-up `silence_*`-style fix if it turns out to be frequent and identifiable.

### 3. Keep `silence_litellm()` as-is, don't remove it

The fd-level redirect is the safety net, not a replacement for silencing what's already known and named. Keep `silence_litellm()` exactly as it is — it stops litellm's own logger from emitting records that would otherwise flow through `logging` normally (and matter for the log *file*, not just the terminal); the fd redirect is specifically about anything that reaches the real terminal by a path structured logging doesn't cover.

## Out of scope for this patch

- The usage/cost attribution bug (separate patch document) — unrelated, don't mix the two fixes into one change.
- Any attempt to enumerate or pre-empt specific third-party output sources beyond what's already in `silence_litellm()` — the whole point of this fix is not needing to do that.

## Acceptance criteria

- [ ] `redirect_fds_to_file` correctly restores the original stdout/stderr fds after the `with` block exits normally.
- [ ] It also correctly restores them when an exception is raised inside the `with` block — test this explicitly (raise inside the context, confirm the terminal's fds are back to normal afterward), since a redirect that isn't restored on a crash would leave the terminal broken for any subsequent command in that shell.
- [ ] With the redirect active, a deliberate raw `print("test")` call placed temporarily inside a worker thread does **not** appear on the terminal while the dashboard is running, and does appear in `stray-output.log` instead.
- [ ] A real `brigade up` session against a live model provider shows no stray text corrupting the dashboard — confirm by actually watching a session run, not just by code inspection.
- [ ] `stray-output.log` is created under `.brigade/logs/`, consistent with where the other log files from the logging phases already live.
- [ ] `silence_litellm()` is still called and still works as before — this patch adds a second layer, it doesn't remove or weaken the first one.
- [ ] `Ctrl+C`/`q` still cleanly stops the dashboard with fds correctly restored — verify the terminal behaves normally (echo, prompt, etc.) immediately after quitting, not just that the process exits.
