"""Live Textual dashboard for `brigade up`."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from brigade.sentinel import sentinel_summary
from brigade.storage import list_inbox, list_ledger
from brigade.tui.events import RoleEvent, UsageEvent
from brigade.tui.summary import summarize_behaviours

ROLES = ["analyst", "examiner", "builder", "designer", "sentinel"]

PIPELINE_DIAGRAM = (
    "Owner (human) ⇄ Interpreter ⇄ Analyst ⇄ Examiner ⇄ Builder\n"
    "                       ⇅\n"
    "                    Designer          Sentinel (audits all)"
)


def _dollar_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Compute cost at render time from litellm's current pricing.

    `cost_per_token` returns the *total* prompt and completion costs for the
    given token counts, so we just sum them.
    """
    import litellm

    prompt_cost, completion_cost = litellm.cost_per_token(
        model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return prompt_cost + completion_cost


def _short(value: str | None) -> str:
    if not value:
        return "—"
    return value[:8]


class OverviewScreen(Screen):
    BINDINGS = [
        Binding("b", "app.push_behaviours", "Behaviours"),
        Binding("f", "app.push_flags", "Flags"),
    ]

    def __init__(self, brigade_dir: Path):
        super().__init__()
        self.brigade_dir = brigade_dir
        self.usage = {
            role: {"model": "", "prompt": 0, "completion": 0} for role in ROLES
        }
        # Roles that logged a terminal event (worker_exit/worker_crashed).  Used
        # by the liveness check to tell a clean exit from a hard kill.
        self.exited_roles: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(PIPELINE_DIAGRAM, id="pipeline")
        yield DataTable(id="roles")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#roles", DataTable)
        table.cursor_type = "row"
        table.add_column("ROLE", key="role")
        table.add_column("STATUS", key="status")
        table.add_column("BEHAVIOUR", key="behaviour")
        table.add_column("COST", key="cost")
        for role in ROLES:
            depth = len(list_inbox(role, self.brigade_dir))
            table.add_row(role, f"{depth} pending", "—", "$0.00", key=role)
        self.set_interval(1.0, self._check_liveness)

    def _check_liveness(self) -> None:
        """Drive the supervisor: restart dead workers and reflect liveness.

        A dead worker is respawned by the supervisor (with backoff); the role
        table shows a distinct RESTARTED flash on respawn, a DOWN-with-countdown
        state while in backoff, and a persistent restart-count badge once a role
        has ever been restarted.
        """
        supervisor = getattr(self.app, "supervisor", None)
        if supervisor is None:
            return
        restarted = supervisor.check_and_restart()
        statuses = supervisor.status()
        table = self.query_one("#roles", DataTable)
        for role, info in statuses.items():
            restarts = info["restarts"]
            if restarts > 0:
                table.update_cell(role, "role", f"{role} ↻{restarts}")
            if role in restarted:
                self.exited_roles.discard(role)
                table.update_cell(
                    role,
                    "status",
                    f"[yellow]↻ RESTARTED (#{restarts})[/yellow]",
                    update_width=True,
                )
                table.update_cell(role, "behaviour", "—")
            elif not info["alive"]:
                table.update_cell(
                    role,
                    "status",
                    f"[red]✖ DOWN — restarting in {info['retry_in']:.0f}s "
                    f"(restart #{restarts})[/red]",
                    update_width=True,
                )
                table.update_cell(role, "behaviour", "—")

    # -- event handlers (called by the App's on_role_event/on_usage_event) ----

    def handle_role_event(self, event: RoleEvent) -> None:
        table = self.query_one("#roles", DataTable)

        if event.event == "consuming":
            status = f"consuming {event.message_type or ''}"
            behaviour = _short(event.behaviour_id)
        elif event.event == "produced":
            status = f"produced {event.message_type or ''} → {event.to_role or ''}"
            behaviour = _short(event.behaviour_id)
        elif event.event == "harness_start":
            status = "harness running"
            behaviour = _short(event.behaviour_id)
        elif event.event == "harness_end":
            status = f"harness done ({event.duration_s:.1f}s)"
            behaviour = _short(event.behaviour_id)
        elif event.event == "harness_failed":
            status = f"[red]HARNESS FAILED[/red] - {event.text or 'no evidence report'}"
            behaviour = "—"
        elif event.event == "committed":
            status = "[green]committed[/green]"
            behaviour = "—"
        elif event.event == "commit_failed":
            status = f"[red]COMMIT FAILED[/red] - {event.text or 'no changes'}"
            behaviour = "—"
        elif event.event == "scan_complete":
            status = f"scan: {event.concern_count} concerns"
            behaviour = "—"
        elif event.event == "worker_start":
            status = "running"
            behaviour = "—"
        elif event.event == "worker_error":
            status = f"[red]ERROR[/red] - {event.text or 'unknown error'}"
            behaviour = "—"
        elif event.event == "worker_crashed":
            status = "[red]CRASHED[/red]"
            behaviour = "—"
            self.exited_roles.add(event.role)
        elif event.event == "worker_exit":
            if event.role in self.exited_roles:
                return  # already terminal (crashed)
            status = "stopped"
            behaviour = "—"
            self.exited_roles.add(event.role)
        elif event.event == "message_recovered":
            status = f"[yellow]recovered[/yellow] {_short(event.message_id)}"
            behaviour = "—"
        else:
            return

        if event.role in table.rows:
            table.update_cell(event.role, "status", status, update_width=True)
            table.update_cell(event.role, "behaviour", behaviour)

    def handle_usage_event(self, event: UsageEvent) -> None:
        role = event.role
        if role not in self.usage:
            return
        usage = self.usage[role]
        usage["model"] = event.model
        usage["prompt"] += event.prompt_tokens
        usage["completion"] += event.completion_tokens

        cost = _dollar_cost(event.model, usage["prompt"], usage["completion"])
        table = self.query_one("#roles", DataTable)
        if role in table.rows:
            table.update_cell(role, "cost", f"${cost:.4f}")


class BehavioursScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("b", "app.pop_screen", "Back"),
    ]

    def __init__(self, brigade_dir: Path):
        super().__init__()
        self.brigade_dir = brigade_dir

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="behaviours")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_list()
        self.set_interval(2.0, self.refresh_list)

    def refresh_list(self) -> None:
        lst = self.query_one("#behaviours", ListView)
        lst.clear()
        for summary in summarize_behaviours(self.brigade_dir):
            bid = summary["behaviour_id"]
            label = (
                f"[{summary['status']}] {_short(bid)} — {summary['stage']}"
                f" → {summary['next_role']}"
            )
            item = ListItem(Static(label))
            item.behaviour_id = bid  # arbitrary attr — ULIDs aren't valid widget ids
            lst.append(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if item is not None:
            bid = getattr(item, "behaviour_id", None)
            if bid:
                self.app.push_screen(LedgerScreen(self.brigade_dir, bid))


class LedgerScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, brigade_dir: Path, behaviour_id: str):
        super().__init__()
        self.brigade_dir = brigade_dir
        self.behaviour_id = behaviour_id
        self._seen: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="ledger", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_ledger()
        self.set_interval(1.0, self.refresh_ledger)

    def refresh_ledger(self) -> None:
        log = self.query_one("#ledger", RichLog)
        for msg in list_ledger(self.brigade_dir, behaviour_id=self.behaviour_id):
            if msg.id in self._seen:
                continue
            self._seen.add(msg.id)
            log.write(f"[{msg.type}] {msg.from_role} → {msg.to_role} ({msg.id[:8]})")


class FlagsScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, brigade_dir: Path):
        super().__init__()
        self.brigade_dir = brigade_dir

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="flags")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_flags()

    def refresh_flags(self) -> None:
        static = self.query_one("#flags", Static)
        counts = sentinel_summary(self.brigade_dir)
        if not counts:
            static.update("No sentinel flags.")
            return
        lines = []
        for (severity, category), count in sorted(counts.items()):
            marker = "▲" if severity == "warning" else "•"
            lines.append(f"{marker} {severity}/{category}: {count}")
        static.update("\n".join(lines))


class BrigadeApp(App):
    TITLE = "Brigade"
    BINDINGS = [Binding("q", "quit", "Quit", priority=True)]

    def __init__(self, brigade_dir: Path, supervisor=None):
        super().__init__()
        self.brigade_dir = brigade_dir
        self.supervisor = supervisor
        self.overview = OverviewScreen(brigade_dir)

    def get_default_screen(self) -> Screen:
        return self.overview

    def on_role_event(self, event: RoleEvent) -> None:
        self.overview.handle_role_event(event)

    def on_usage_event(self, event: UsageEvent) -> None:
        self.overview.handle_usage_event(event)

    def action_push_behaviours(self) -> None:
        self.push_screen(BehavioursScreen(self.brigade_dir))

    def action_push_flags(self) -> None:
        self.push_screen(FlagsScreen(self.brigade_dir))


def run_dashboard(brigade_dir: Path, supervisor=None) -> None:
    """Launch the dashboard (blocking)."""
    BrigadeApp(brigade_dir, supervisor=supervisor).run()
