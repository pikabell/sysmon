"""Application shell: tab bar, panel routing and the help overlay."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Static
from rich.text import Text

from .core.context import SOURCES
from .core.panel import Panel
from .core.registry import PANELS
from .panels.gpu import GPUPanel
from .panels.processes import ProcessesPanel
from .panels.overview import OverviewPanel
from .config import Settings, config_path
from .history import History
from .screens import (
    ExportScreen,
    HistoryScreen,
    METRIC_HELP,
    ProcessScreen,
    SettingsScreen,
    ViewerScreen,
)
from .core.render import fmt_uptime

HELP_TEXT = """\
[b]sysmon[/b] - panels

  [b]0[/b] Overview   everything notable on one screen
  [b]1[/b] CPU        aggregate and per-core utilisation
  [b]2[/b] Memory     RAM, cache and swap
  [b]3[/b] Processes  every process with its [b]exact start time[/b]
  [b]4[/b] GPU        per-device telemetry and per-process GPU usage
  [b]5[/b] Network    interface throughput and socket owners
  [b]6[/b] Disk       device I/O, free space and inodes
  [b]7[/b] Resources  pressure, cgroup quotas and system uptime

[b]switching panels[/b]
  [b]0[/b]-[b]7[/b]        jump straight to a panel
  [b]left[/b] / [b]right[/b]  previous / next panel
  [b]h[/b] / [b]l[/b]      same, vi-style
  shift+left / shift+right   scroll wide tables horizontally
  up / down / PgUp / PgDn    scroll the current view

[b]processes[/b]
  [b]/[/b]        focus the filter box
  [b]enter[/b]     apply the filter and return to the table
  [b]esc[/b]       leave the filter box
  [b]s[/b]        cycle the sort column
  [b]S[/b]        reverse the sort direction
  [b]c[/b]        announce the current sort column
  click a header to sort by it

[b]elsewhere[/b]
  [b]g[/b]        toggle compute-only GPU processes
  [b]enter / d[/b] inspect selected process or GPU job
  [b]H[/b]        expand session history
  [b]n[/b]        open all overview threshold notices
  [b]i[/b]        explain metrics in the current panel
  [b],[/b]        saved settings
  [b]e[/b]        export a JSON snapshot (commands omitted by default)
  [b]r[/b]        sample again now
  [b]?[/b]        this help
  [b]q[/b]        quit

Arrows and digits switch panels from tables; inside the filter they edit text. Hidden
panels stop sampling, so only the visible tab costs anything.
"""


class HelpScreen(ModalScreen[None]):
    """Modal help overlay."""

    BINDINGS = [
        Binding("escape,q,question_mark", "dismiss", "Close", show=False),
        Binding("space,enter", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = ""  # styling lives in sysmon.tcss

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(HELP_TEXT.rstrip("\n"), id="help-title")

    def on_mount(self) -> None:
        # Centre the box once its content is known, so the border hugs the text.
        self.query_one("#help-box").focus()

    def action_dismiss(self) -> None:
        self.dismiss(None)


def _panel_id(panel_class) -> str:
    """DOM id for a panel.

    Textual ids may not begin with a digit, and our activation keys are digits, so
    they are namespaced rather than used bare.
    """
    return f"panel-{panel_class.KEY}"


class SysmonApp(App[None]):
    """A tabbed system monitor."""

    CSS_PATH = "sysmon.tcss"
    TITLE = "sysmon"
    #: Nothing auto-focuses on launch. Without this, the *hidden* Processes panel's
    #: filter Input steals focus, swallows every digit key, and DataTable then eats
    #: the arrows. Focus is managed explicitly by :meth:`_focus_current_panel`.
    AUTO_FOCUS = None
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help"),
        Binding("r", "refresh", "Refresh"),
        Binding("shift+left", "table_left", show=False),
        Binding("shift+right", "table_right", show=False),
        # Priority so panel switching works even while a DataTable has focus:
        # DataTable otherwise binds left/right for cursor movement.
        Binding("left", "prev_panel", "Prev", priority=True, show=False),
        Binding("right", "next_panel", "Next", priority=True, show=False),
        Binding("h", "prev_panel", "Prev", show=False),
        Binding("l", "next_panel", "Next", show=False),
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "release_focus", "Leave filter", show=False),
        Binding("s", "cycle_sort", "Sort by"),
        Binding("S", "reverse_sort", "Reverse"),
        Binding("c", "show_sort", "Sort column", show=False),
        Binding("g", "toggle_gpu", "GPU kind"),
        Binding("d", "process_details", "Details"),
        Binding("H", "history", "History"),
        Binding("i", "metric_help", "Explain"),
        Binding("n", "notices", "Notices"),
        Binding("comma", "settings", "Settings"),
        Binding("e", "export", "Export"),
    ]

    def __init__(
        self, settings: Settings | None = None, settings_path: Path | None = None
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.settings_path = (
            settings_path if settings_path is not None else config_path()
        )
        self.history = History(self.settings.history_samples)
        self.history.set_hidden(self.settings.hidden_interfaces)
        if self.settings.theme not in self.available_themes:
            raise ValueError(f"unknown theme: {self.settings.theme}")
        self.theme = self.settings.theme
        self._active_index = 0
        self._pending_panel: int | None = int(self.settings.default_panel)

    def open_panel_on_start(self, index: int) -> None:
        """Choose the panel to show at launch (``--panel``)."""
        self._pending_panel = index % len(PANELS)

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(Text("sysmon · System uptime: collecting…"), id="system-header")
        with HorizontalScroll(id="tabs"):
            for index, panel in enumerate(PANELS):
                yield Static(
                    f"{panel.KEY} {panel.TITLE}",
                    id=f"tab-{panel.KEY}",
                    classes="tab",
                )
        with ContentSwitcher(initial=_panel_id(PANELS[0]), id="panels"):
            for panel_class in PANELS:
                yield panel_class(id=_panel_id(panel_class))
        yield Footer()

    def on_mount(self) -> None:
        for panel in self.query(Panel):
            panel.INTERVAL = self.settings.refresh_interval
            panel.settings = self.settings
        self._refresh_uptime()
        self.set_interval(1, self._refresh_uptime)
        # Rate-based sources need two samples before they report anything, so warm
        # them in the background. Otherwise the first painted frame is all zeroes.
        self.run_worker(
            self._start_sampling(),
            name="warm",
            group="sysmon-warm",
            exclusive=True,
        )

        # Honour --panel, which sets _active_index before mount.
        if self._pending_panel:
            self._active_index = self._pending_panel
            self.query_one("#panels", ContentSwitcher).current = _panel_id(
                PANELS[self._active_index]
            )
        self._mark_active()
        self._focus_current_panel()

    async def _start_sampling(self) -> None:
        await asyncio.to_thread(SOURCES.warm)
        # Give rate counters a useful interval after establishing their baseline.
        await asyncio.sleep(0.2)
        self.current.activate()

    def _panel(self, index: int) -> Panel:
        return self.query_one(f"#{_panel_id(PANELS[index % len(PANELS)])}", Panel)

    # -- routing -----------------------------------------------------------

    def on_key(self, event) -> None:
        """Route bare digit keys to panels."""
        if isinstance(self.focused, Input) or len(self.screen_stack) > 1:
            return
        if event.key.isdigit() and event.key in {p.KEY for p in PANELS}:
            event.stop()
            event.prevent_default()
            index = [p.KEY for p in PANELS].index(event.key)
            self.show_panel(index)

    def action_show_panel(self, index: int) -> None:
        """Binding entry point for selecting a panel by index."""
        self.show_panel(index)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if len(self.screen_stack) > 1:
            return False
        if action in {"focus_filter", "cycle_sort", "reverse_sort"} and not isinstance(
            self.current, ProcessesPanel
        ):
            return None
        if action == "toggle_gpu" and not isinstance(self.current, GPUPanel):
            return None
        if action == "notices" and not isinstance(self.current, OverviewPanel):
            return None
        if action == "process_details" and not isinstance(
            self.current, (ProcessesPanel, GPUPanel)
        ):
            return None
        if isinstance(self.focused, Input) and action in ("prev_panel", "next_panel"):
            return False
        return True

    def show_panel(self, index: int) -> None:
        """Make panel ``index`` visible and start sampling it."""
        index %= len(PANELS)
        target = self._panel(index)
        if target is self._panel(self._active_index):
            return
        previous = self._panel(self._active_index)
        # Drop focus before hiding the old panel. Otherwise a widget inside it
        # keeps the keyboard and swallows the very keys that switched panels.
        if self.focused is not None and self.focused in previous.query("*"):
            self.set_focus(None)
        previous.deactivate()
        self.query_one("#panels", ContentSwitcher).current = target.id
        if SOURCES.warmed:
            target.activate()
        self._active_index = index
        self._mark_active()
        self.refresh_bindings()
        # ContentSwitcher has only just flipped display, so the new panel's
        # children still report their *old* regions. Focus after layout settles.
        self.call_after_refresh(self._focus_current_panel)

    def _focus_current_panel(self) -> None:
        """Give the visible panel's table focus so scrolling and keys work.

        Without this the previous panel keeps focus, and its widgets continue to
        consume keys for a panel that is no longer on screen.
        """
        panel = self._panel(self._active_index)
        tables = list(panel.query(DataTable))
        if tables:
            # The largest table holds the rows the user came to read, and is what
            # the arrow keys should scroll. Regions can still be stale for a
            # panel that has just become visible, so pick by size but tolerate
            # a zero-height measurement rather than giving up.
            target = max(tables, key=lambda table: table.region.height)
            if target.region.height == 0 and len(tables) > 1:
                target = tables[-1]
            target.focus()
            return
        # CPU and Memory have no table; focus the panel so it can scroll.
        panel.focus()

    def _mark_active(self) -> None:
        for index, panel_class in enumerate(PANELS):
            tab = self.query_one(f"#tab-{panel_class.KEY}", Static)
            tab.update(self.tab_label(panel_class, self.size.width))
            tab.set_class(index == self._active_index, "tab--active")
            if index == self._active_index:
                tab.call_after_refresh(tab.scroll_visible, animate=False)

    @staticmethod
    def tab_label(panel_class, width: int) -> str:
        short = {"Processes": "Proc", "Network": "Net", "Resources": "Limits"}
        title = (
            short.get(panel_class.TITLE, panel_class.TITLE)
            if width < 110
            else panel_class.TITLE
        )
        return f"{panel_class.KEY} {title}"

    def on_resize(self) -> None:
        if self.is_mounted:
            self._mark_active()

    def on_click(self, event) -> None:
        widget = event.widget
        if (
            widget is not None
            and widget.id
            and widget.id.startswith("tab-")
            and len(self.screen_stack) == 1
        ):
            key = widget.id[4:]
            self.show_panel([panel.KEY for panel in PANELS].index(key))

    def _refresh_uptime(self) -> None:
        self.run_worker(self._uptime(), group="uptime", exclusive=True)

    async def _uptime(self) -> None:
        try:
            raw = await asyncio.to_thread(Path("/proc/uptime").read_text)
            value = float(raw.split()[0])
            if not 0 <= value < float("inf"):
                raise ValueError("invalid uptime")
            self._set_uptime(value)
        except (OSError, ValueError, IndexError):
            self._set_uptime(None)

    def _set_uptime(self, value: float | None) -> None:
        self.sub_title = f"system uptime {fmt_uptime(value)}"
        if not self.screen_stack:
            return
        headers = list(self.screen_stack[0].query("#system-header"))
        if not headers:
            return
        headers[0].update(
            Text.assemble(
                ("sysmon", "bold"),
                ("  ·  System uptime ", "grey62"),
                (fmt_uptime(value), "cyan"),
            )
        )

    @property
    def current(self) -> Panel:
        return self._panel(self._active_index)

    # -- actions -----------------------------------------------------------

    def action_prev_panel(self) -> None:
        self.show_panel((self._active_index - 1) % len(PANELS))

    def action_next_panel(self) -> None:
        self.show_panel((self._active_index + 1) % len(PANELS))

    def action_refresh(self) -> None:
        self.current.refresh_now()
        self.notify("resampling", timeout=1)

    def _scroll_table(self, amount: int) -> None:
        tables = list(self.current.query(DataTable))
        if tables:
            table = self.focused if isinstance(self.focused, DataTable) else tables[-1]
            table.scroll_relative(x=amount, animate=False)

    def action_table_left(self) -> None:
        self._scroll_table(-20)

    def action_table_right(self) -> None:
        self._scroll_table(20)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_focus_filter(self) -> None:
        panel = self.current
        if isinstance(panel, ProcessesPanel):
            self.query_one("#proc-filter", Input).focus()
        else:
            self.notify("the filter applies to the Processes panel (press 3)")

    def action_release_focus(self) -> None:
        """Escape leaves the filter box and returns control to the panel."""
        focused = self.focused
        if isinstance(focused, Input):
            focused.blur()
            panel = self.current
            panel.apply_filter(focused.value)
            self._focus_current_panel()

    def action_cycle_sort(self) -> None:
        panel = self.current
        if isinstance(panel, ProcessesPanel):
            panel.cycle_sort()
            self.notify(f"sort: {panel.sort_key}")
        else:
            self.notify("sorting applies to the Processes panel (press 3)")

    def action_reverse_sort(self) -> None:
        panel = self.current
        if isinstance(panel, ProcessesPanel):
            panel.sort_by(panel.sort_key)
            self.notify(f"{panel.sort_key} {'desc' if panel.reverse else 'asc'}")

    def action_show_sort(self) -> None:
        panel = self.current
        if isinstance(panel, ProcessesPanel):
            direction = "descending" if panel.reverse else "ascending"
            self.notify(f"sorting by {panel.sort_key}, {direction}")
        elif isinstance(panel, GPUPanel):
            self.notify("gpu processes are ordered by vram, largest first")
        else:
            self.notify(f"{panel.TITLE}: no sortable columns")

    def action_toggle_gpu(self) -> None:
        panel = self.current
        if isinstance(panel, GPUPanel):
            panel.toggle_kind()
            self.notify(
                "compute processes only" if panel.compute_only else "all GPU processes"
            )
        else:
            self.notify("that toggle applies to the GPU panel (press 4)")

    def action_process_details(self) -> None:
        panel = self.current
        if isinstance(panel, (ProcessesPanel, GPUPanel)):
            info = panel.selected_process()
            if info is not None:
                self.push_screen(ProcessScreen(info))
            else:
                self.notify("No readable process is selected")
        else:
            self.notify("Select a process on panel 3 or 4")

    def action_history(self) -> None:
        self.push_screen(HistoryScreen())

    def action_notices(self) -> None:
        panel = self.current
        if isinstance(panel, OverviewPanel) and panel.last_summary is not None:
            alerts = panel.last_summary.alerts
            self.push_screen(
                ViewerScreen(
                    "Threshold notices · Esc closes",
                    "\n\n".join(alert.message for alert in alerts)
                    or "No thresholds exceeded",
                )
            )

    def action_metric_help(self) -> None:
        self.push_screen(
            ViewerScreen(
                f"{self.current.TITLE} · metric explanations · Esc closes",
                METRIC_HELP[self.current.KEY],
            )
        )

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(), self._apply_settings)

    def _apply_settings(self, settings: Settings | None) -> None:
        if settings is None:
            return
        self.settings = settings
        self.theme = settings.theme
        self.history.resize(settings.history_samples)
        self.history.set_hidden(settings.hidden_interfaces)
        for panel in self.query(Panel):
            panel.INTERVAL = settings.refresh_interval
            panel.settings = settings
        self.current.refresh_now()
        self.notify(f"Saved settings to {self.settings_path}")

    def action_export(self) -> None:
        self.push_screen(ExportScreen())
