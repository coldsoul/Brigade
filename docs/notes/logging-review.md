# Relay `up` — Logging Review & Improvement Options

This document reviews the logging produced by `relay up` and lists options for improving it.
It is a proposal, not an implementation plan — nothing here is implemented yet.

## Current state

All logging is raw `print()` / `click.echo()` with no logging framework.
The typical output looks like:

```
Relay workers started: analyst, examiner, builder, designer, sentinel
Press Ctrl+C to stop.
[builder] consuming expectation (01M0JVN2JDNJAQXGQCT1EYKGK5)
[builder] harness exited 0
[builder] harness stdout: <...up to 2000 chars...>
[builder] harness stderr: <...up to 2000 chars...>
[builder] produced evidence → examiner (01M0JWHXKWPVBG5H3VQ30NZG31)
[sentinel] emitted advisory with 2 concern(s)
```

## Pain points

1. **No timestamps** — you cannot tell whether a worker is slow or hung, and latency is not measurable.
2. **No severity levels** — informational lines (`consumed`/`produced`) are indistinguishable from warnings (`no valid evidence written`) and debug (harness `stderr`).
3. **Harness output dumped inline** — the Builder/Designer dump up to 2000 chars of `stdout`/`stderr` into the stream, arbitrarily truncated and drowning the signal.
4. **No correlation IDs** — `behaviour_id`/`message_id` appear only sporadically, so one behaviour's journey cannot be traced with `grep`.
5. **Thread interleaving** — five worker threads print concurrently with no atomicity, so lines from different roles collide.
6. **Nothing persisted** — logs go to stdout only; the ledger is the *audit* trail, not the *operational* log.
7. **No verbosity control** — the harness dumps cannot be silenced, and debug detail cannot be increased.

## Options

### A — Standard library `logging` (foundation)
Replace `print` with `logging`, one logger per role/module, a formatter like `%(asctime)s %(levelname)s [%(name)s] %(message)s`, and levels `DEBUG`/`INFO`/`WARNING`/`ERROR`.
- *Cost:* low.
- *Gain:* timestamps, levels, and filtering for free. This is the base every other option builds on.

### B — Verbosity flags
`relay up --quiet` (warnings/errors only) / `--verbose` (`DEBUG`) / default `INFO`.
- *Cost:* low.
- *Gain:* harness dumps become `DEBUG`-only, so normal runs stay clean.

### C — Correlation IDs
Thread `behaviour_id` and `message_id` into every worker log line (e.g. `[builder bid=… msg=…]`).
Then `grep bid=01M0JVME…` replays one behaviour's whole path.
- *Cost:* low–medium (plumb the ids through).
- *Gain:* the single biggest tracing win, independent of anything else.

### D — Harness output to files, not the stream
Stop inline-dumping `stdout`/`stderr`.
Write the full harness output to a per-run file (e.g. `.relay/logs/builder-<msg_id>.log`), and log a one-line summary plus the file path.
- *Cost:* low.
- *Gain:* a readable terminal, full output preserved with no truncation, still inspectable on failure.

### E — Structured/JSON logging
Emit one JSON object per event (`{ts, level, role, event, behaviour_id, message_id, …}`), with an optional human-readable mode.
- *Cost:* medium.
- *Gain:* machine-parseable output (`jq`, `grep`) and post-hoc analysis; pairs naturally with C.

### F — Log file + rotation
Route logs to `.relay/logs/relay.log` (or per-role files) with a `RotatingFileHandler`, in addition to the console.
- *Cost:* low.
- *Gain:* operational history survives terminal exit and answers "what happened while I was away."

### G — Timing/latency instrumentation
Log durations on key hops ("builder harness took 43s", "examiner 2.1s").
- *Cost:* low (wrap timers).
- *Gain:* directly answers "is it slow or hung?" and makes latency regressions measurable.

### H — Live status TUI (dashboard)
Keep detailed logs in a file and render a live high-level panel (inbox depths, in-flight behaviour, current role activity, sentinel flag count) refreshed in place — effectively `relay status`, but live.
- *Cost:* medium–high.
- *Gain:* a "what is happening right now" view decoupled from the noisy event stream; best as a later layer on top of A–D.

## Suggested path

A + C + D + G are the high-value, low-cost core: the `logging` foundation, correlation IDs, harness output to files, and timestamps/durations.
Then B (verbosity flags) falls out for free, and E (JSON) or H (TUI) are optional layers on top.
