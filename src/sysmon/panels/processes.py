"""Processes panel: the full process table with start times.

Two things here are worth calling out:

* **Start time is exact.** ``STARTED`` is wall-clock, derived from the kernel's
  ``starttime`` plus boot time, and ``AGE`` is derived from the same value. Both
  stay correct no matter when sysmon was launched, so an 8-day-old process always
  reads 8 days rather than counting up from zero.
* **CPU% is a delta.** It is computed from ``utime + stime`` changes between
  samples, so it is meaningful for processes that were already running when the
  monitor started.
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static
from rich.text import Text

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import fmt_start, human_duration
from ..core.table import TableSync
from ..sources.procs import ProcInfo

# (column key, label, width) - a None width lets the column flex.
COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("pid", "PID", 8),
    ("user", "USER", 10),
    ("cpu", "CPU%", 7),
    ("mem", "MEM%", 7),
    ("rss", "RSS", 9),
    ("thr", "THR", 5),
    ("st", "ST", 8),
    ("age", "AGE", 10),
    ("start", "STARTED", 13),
    ("cmd", "COMMAND", None),
)

# Column keys in display order, for the sort-cycle binding.
SORT_CYCLE = ("cpu", "mem", "pid", "user", "rss", "thr", "st", "age", "start", "cmd")

# Dim the boring states so "Running" stands out in a 650-row table.
STATE_STYLES = {
    "R": "bold green",
    "D": "yellow",
    "Z": "bold red",
    "T": "magenta",
    "I": "grey42",
}

# How often the sort order is recomputed. Values update every tick regardless;
# re-sorting every tick would make rows jump around under the cursor.
RESORT_INTERVAL = 2.0


def _sort_value(info: ProcInfo, key: str):
    if key == "pid":
        return info.pid
    if key == "user":
        return info.username
    if key == "cpu":
        return info.cpu_pct
    if key == "mem":
        return info.rss_bytes
    if key == "rss":
        return info.rss_bytes
    if key == "thr":
        return info.threads
    if key == "st":
        return info.state
    if key == "age":
        return info.age if info.age is not None else -1
    if key == "start":
        return info.start_ts if info.start_ts is not None else -1
    return info.cmdline


class ProcessesPanel(Panel):
    KEY = "3"
    TITLE = "Processes"
    INTERVAL = 1.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = SOURCES.proc
        self.total_memory: int | None = None
        self.sort_key = "cpu"
        self.reverse = True
        self.filter_text = ""
        self._sync: TableSync | None = None
        self._last_resort = 0.0
        self._matched = 0
        self._seen = 0
        self._hint = ""
        self._latest: dict[int, ProcInfo] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(
                placeholder="filter PID, user, command, state or start time - Enter/Esc to apply",
                id="proc-filter",
            )
            yield DataTable(id="proc-table", zebra_stripes=True, cursor_type="row")
            yield Static("", id="proc-summary")
        yield Static("", id="status", classes="statusline")

    def on_mount(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        for key, label, width in COLUMNS:
            table.add_column(label, key=key, width=width)
        self._sync = TableSync(table, [(key, label) for key, label, _ in COLUMNS])
        table.cursor_type = "row"

    # -- sampling ----------------------------------------------------------

    def fetch(self) -> dict[int, ProcInfo]:
        try:
            self.total_memory = SOURCES.mem.sample().total
        except OSError:
            self.total_memory = None
        return self.source.sample()

    def apply(self, table: dict[int, ProcInfo]) -> None:
        self._latest = table
        if self._sync is None:
            return
        needle = self.filter_text.strip().lower()
        rows: dict[int, tuple] = {}
        for pid, info in table.items():
            if needle and not self._matches(info, needle):
                continue
            rows[pid] = self._cells(info)

        now = time.monotonic()
        resort = now - self._last_resort >= RESORT_INTERVAL
        if resort:
            self._last_resort = now
            order = self._sorted_order(rows, table)
        else:
            # Keep the previous ordering so the cursor stays put.
            order = self._sync.order

        self._sync.sync(rows, order)
        self._seen = len(table)
        self._matched = len(rows)
        self._update_summary()

    @staticmethod
    def _matches(info: ProcInfo, needle: str) -> bool:
        return (
            needle in info.cmdline.lower()
            or needle in info.name.lower()
            or needle in info.username.lower()
            or needle in str(info.pid)
            or needle in info.state_label.lower()
            or needle in fmt_start(info.start_ts).lower()
        )

    def _cells(self, info: ProcInfo) -> tuple:
        mem_pct = info.rss_bytes / self.total_memory if self.total_memory else None
        return (
            Text(str(info.pid), style="grey70"),
            Text(info.username),
            Text(f"{info.cpu_pct:>5.1f}%", style="cyan"),
            Text("-" if mem_pct is None else f"{mem_pct * 100:>5.1f}%", style="cyan"),
            f"{info.rss_bytes / 2**20:>7.0f}M",
            str(info.threads),
            Text(info.state_label, style=STATE_STYLES.get(info.state, "white")),
            Text(human_duration(info.age), style="grey70"),
            Text(fmt_start(info.start_ts), style="grey70"),
            Text(info.cmdline),
        )

    def _sorted_order(
        self, rows: dict[int, tuple], table: dict[int, ProcInfo]
    ) -> list[int]:
        return sorted(
            rows,
            key=lambda pid: (_sort_value(table[pid], self.sort_key), pid),
            reverse=self.reverse,
        )

    def _update_summary(self) -> None:
        direction = "descending" if self.reverse else "ascending"
        summary = self.query_one("#proc-summary", Static)
        summary.remove_class("error")
        if self.filter_text.strip():
            summary.add_class("error")

        text = Text()
        text.append(f"{self._matched}", style="bold")
        text.append(f" of {self._seen} processes", style="grey62")
        text.append("  ·  ", style="grey35")
        text.append("sorted by ", style="grey62")
        text.append(self.sort_key, style="bold")
        text.append(f" {direction}", style="grey62")
        if self.filter_text.strip():
            text.append("  ·  ", style="grey35")
            text.append(f"filter: {self.filter_text}", style="yellow")
        summary.update(text)

    def status_prefix(self) -> str:
        return ""

    # -- interaction -------------------------------------------------------

    def sort_by(self, key: str) -> None:
        """Sort by ``key``; re-selecting the same key reverses the direction."""
        if key not in SORT_CYCLE:
            return
        if key == self.sort_key:
            self.reverse = not self.reverse
        else:
            self.sort_key = key
            # Highest values first for magnitude columns, lowest PID first otherwise.
            self.reverse = key in ("cpu", "mem", "rss", "thr", "age")
        self._force_resort()

    def cycle_sort(self) -> None:
        index = SORT_CYCLE.index(self.sort_key)
        self.sort_by(SORT_CYCLE[(index + 1) % len(SORT_CYCLE)])

    def _force_resort(self) -> None:
        self._last_resort = 0.0
        if self._sync is not None:
            self._sync.invalidate()
            self.apply(self._latest)

    def apply_filter(self, value: str) -> None:
        self.filter_text = value
        self._force_resort()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter commits the filter and hands key handling back to the panel.
        self.apply_filter(event.value)
        event.input.blur()
        app = self.app
        if hasattr(app, "_focus_current_panel"):
            app._focus_current_panel()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        key = event.column_key.value
        self.sort_by(key)

    def selected_process(self) -> ProcInfo | None:
        table = self.query_one("#proc-table", DataTable)
        if not table.row_count:
            return None
        pid = int(str(table.get_row_at(table.cursor_coordinate.row)[0]))
        return self._latest.get(pid)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.action_process_details()
