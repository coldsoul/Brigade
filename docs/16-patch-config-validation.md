# Patch A — Fail-Fast Config Validation at Startup

Assumes current `main` (post timeout patch). This is the higher-leverage half of fixing a silent-failure class that just cost a 30-minute debugging session: `brigade up` starts happily with an unconfigured or misconfigured model, and the problem only surfaces — invisibly, inside a worker thread — minutes later when a role first tries to call its model. Everything checkable up front should be checked up front, before any thread starts or the TUI launches.

## The bug

`config.py` already has the pieces (a `RoleConfig.model: str | None`, a `ConfigError` type, a `BUILTIN_CAPABILITIES` provider table, and a `_split_provider` helper in `capabilities.py`) but nothing rejects a role whose `model` is `None`, malformed, or whose provider's API key is absent from the environment. `Config.model_validate` only enforces the builder-harness rule today. So a `config.toml` that never configured the analyst's model passes validation, `brigade up` launches, and the failure is deferred to a silent spot deep in a daemon thread.

## The fix

Add an explicit `validate_runtime_config(config)` that runs at the very top of the `up` command — before workers start, before the TUI launches — and exits with a clear, specific message and nonzero code on any problem. This is separate from pydantic's structural validation (which runs at load); this is about runtime-readiness (models present, providers known, keys available).

### 1. Provider → env-var mapping

In `capabilities.py` (where provider knowledge already lives), add the standard env var each supported provider's key comes from — LiteLLM's own conventions:

```python
# provider → environment variable holding its API key
PROVIDER_ENV_VAR = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
```

Scope this to the providers brigade actually supports today (the same Anthropic/OpenAI/Deepseek set the capability table is scoped to, plus openrouter since it's a documented config path). For a provider not in this map, the validator should *warn* (unknown provider — can't verify its key) rather than hard-fail, since brigade explicitly allows unknown-provider models to work through config with a conservative fallback — don't turn "we don't know this provider's key convention" into a launch-blocking error.

### 2. `validate_runtime_config`

New function in `config.py` (or a small `config_validation.py` — keep it near the config code):

```python
def validate_runtime_config(config: Config) -> None:
    """Check every role brigade will actually run is ready to make model calls.

    Raises ConfigError (with all problems collected, not just the first) if any
    role has no model, a malformed model string, or a known provider whose API
    key is missing from the environment. Warnings (unknown provider) are logged
    but do not raise.
    """
    problems: list[str] = []
    warnings: list[str] = []

    # The roles that make model calls and must be configured to run.
    # Interpreter is the harness itself (not a brigade worker) — exclude it.
    required_roles = ["analyst", "examiner", "builder", "designer", "sentinel"]

    for role in required_roles:
        role_cfg = config.roles.get(role)
        model = role_cfg.model if role_cfg else None

        if not model:
            problems.append(f"{role}: no model configured in [roles.{role}]")
            continue

        if "/" not in model:
            problems.append(
                f"{role}: model {model!r} is not in 'provider/model' form "
                f"(e.g. 'anthropic/claude-sonnet-5')"
            )
            continue

        provider = model.split("/", 1)[0]
        env_var = PROVIDER_ENV_VAR.get(provider)
        if env_var is None:
            warnings.append(
                f"{role}: provider {provider!r} is not one brigade knows the key "
                f"convention for — can't verify its API key is set"
            )
        elif not os.environ.get(env_var):
            problems.append(
                f"{role}: {env_var} is not set (required for model {model})"
            )

    for w in warnings:
        logging.getLogger("brigade").warning(w)

    if problems:
        raise ConfigError(
            "Configuration problems prevent startup:\n  - "
            + "\n  - ".join(problems)
        )
```

Notes on the choices here:
- **Collect all problems, don't stop at the first.** A user with three unconfigured roles should see all three at once, not fix-rerun-repeat three times.
- **Which roles are "required" depends on which the user actually intends to run.** If designer/sentinel are ones a given project won't use, hard-failing on their absence is wrong. Reconcile against how `brigade up` currently decides which workers to start — if it always starts all of them, validate all of them; if some are opt-in, only validate the ones that will actually run. Match the validator's `required_roles` to that reality rather than assuming all five.
- **Interpreter is deliberately excluded** — it's the harness (opencode/Claude Code), not a brigade-run worker with a `[roles.interpreter]` model.

### 3. Wire it into `brigade up`

In `cli.py`, at the very start of `up`, after `load_config` but before anything else:

```python
try:
    config = load_config(brigade_dir)
    validate_runtime_config(config)
except ConfigError as exc:
    click.echo(f"error: {exc}", err=True)
    raise SystemExit(1)
```

This runs before `configure_logging`, before the fd-redirect, before any worker thread, before `app.run()` — so the error prints plainly to the real terminal, not into a log file or under a TUI that's already taken the screen.

## Out of scope for this patch

- Runtime worker-error surfacing (that's Patch B — this patch is only about what's checkable *before* launch).
- Validating that a key is *correct*/accepted by the provider — can't be done without a live call; this only checks presence. A wrong-but-present key is Patch B's territory (it'll surface as a runtime auth error).
- Adding new providers to the capability/env-var tables beyond the currently-supported set.

## Acceptance criteria

- [ ] `brigade up` with a `config.toml` missing the analyst's model exits immediately, before the TUI launches, with a message naming the analyst specifically and a nonzero exit code — verify the exact scenario that caused the original silent hang.
- [ ] Multiple problems are reported together in one run, not one-at-a-time across reruns.
- [ ] A role whose model references a provider whose API-key env var is unset fails with a message naming both the env var and the role.
- [ ] A model string without a `/` fails with a message showing the expected form.
- [ ] An unknown/custom provider produces a logged warning but does **not** block startup — confirm brigade still launches in that case.
- [ ] The `required_roles` list matches which workers `brigade up` actually starts — if some roles are opt-in, an unused one being unconfigured does not block startup.
- [ ] The error prints to the terminal (stderr), not into `brigade.log` or `stray-output.log` — confirm it's visible in a normal shell run.
- [ ] A fully, correctly configured project still starts normally with no spurious errors or warnings.
