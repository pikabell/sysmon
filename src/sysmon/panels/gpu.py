"""GPU panel: per-device telemetry plus the processes holding GPU memory.

The per-process table is the point of this panel: it answers "what is using my
GPU, who started it, and how long has it been running". Each row carries the same
user, command and start time the process panel shows, so a GPU job is traceable
across panels.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from rich.panel import Panel as Card
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import bar, human_bytes, human_duration, fmt_timestamp, heat, pct
from ..core.table import TableSync
from ..sources.gpu import GPUSnapshot
from ..sources.nvml import GPUDevice

PROC_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("pid", "PID", 8),
    ("start", "STARTED (LOCAL)", 19),
    ("user", "USER", 10),
    ("gpu", "GPU", 5),
    ("type", "TYPE", 6),
    ("sm", "SM%", 6),
    ("mem", "MEM IO%", 7),
    ("vram", "VRAM", 11),
    ("age", "AGE", 10),
    ("cmd", "COMMAND", None),
)

# C = compute kernel, G = graphics/display.
TYPE_STYLES = {"C": "bold cyan", "G": "bold magenta", "C+G": "bold cyan", "?": "grey58"}

MIB = 1024 * 1024

# A card needs roughly 5 (label) + 14 (bar) + 20 (value) plus padding. Budgeting
# 46 per card stops the values being truncated when several fit side by side.
CARD_WIDTH = 46


class GPUPanel(Panel):
    KEY = "4"
    TITLE = "GPU"
    INTERVAL = 1.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = SOURCES.gpu
        self.compute_only = False
        self._sync: TableSync | None = None
        self._latest: GPUSnapshot | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="gpu-scroll"):
                yield Static(id="gpu-devices")
            yield Static(id="gpu-note")
            yield DataTable(id="gpu-procs", zebra_stripes=True, cursor_type="row")
        yield Static("", id="status", classes="statusline")

    def on_mount(self) -> None:
        table = self.query_one("#gpu-procs", DataTable)
        for key, label, width in PROC_COLUMNS:
            table.add_column(label, key=key, width=width)
        self._sync = TableSync(table, [(key, label) for key, label, _ in PROC_COLUMNS])

    def fetch(self) -> GPUSnapshot:
        return self.source.sample()

    def apply(self, snapshot: GPUSnapshot) -> None:
        self._latest = snapshot
        self.query_one("#gpu-devices", Static).update(self._devices(snapshot))
        self.query_one("#gpu-note", Static).update(self._note(snapshot))
        if self._sync is not None:
            self._sync.sync(self._rows(snapshot), self._order(snapshot))

    def on_resize(self) -> None:
        if self._latest is not None and self.is_mounted:
            self.query_one("#gpu-devices", Static).update(self._devices(self._latest))

    def status_prefix(self) -> str:
        return ""

    # -- devices -----------------------------------------------------------

    def _devices(self, snapshot: GPUSnapshot) -> Table:
        if not snapshot.devices:
            reason = snapshot.error or "no supported GPU devices"
            box = Table.grid(padding=(0, 1))
            box.add_column()
            box.add_row(Text(reason, style="grey58"))
            return box

        cards = Table.grid(padding=(0, 2))
        cards.add_column()

        # Budget the panel's own padding out first, then lay out only as many
        # cards per row as genuinely fit, so no value gets truncated.
        available = max(1, (self.size.width or 100) - 4)
        columns = max(1, min(len(snapshot.devices), available // CARD_WIDTH))
        # "465.8 MiB / 16.0 GiB" is 20 columns; 5 for the label plus padding.
        bar_width = max(4, min(14, available // columns - 26))

        header = Text()
        header.append(f"driver {snapshot.driver or 'unknown'}", style="grey58")
        header.append("  ·  ", style="grey35")
        header.append(f"{len(snapshot.devices)} device(s)", style="grey58")
        header.append("  ·  ", style="grey35")
        header.append(f"source {snapshot.source}", style="grey58")
        cards.add_row(header)

        cards_by_column = max(1, (available - (columns - 1) * 2) // columns)
        grid = Table.grid(padding=(0, 2))
        for _ in range(columns):
            grid.add_column(no_wrap=True, width=cards_by_column)

        devices = list(snapshot.devices)
        for start in range(0, len(devices), columns):
            chunk = devices[start : start + columns]
            row = [self._card(device, bar_width, cards_by_column) for device in chunk]
            while len(row) < columns:
                row.append(Text(""))
            grid.add_row(*row)
        cards.add_row(grid)
        return cards

    def _card(self, device: GPUDevice, bar_width: int = 14, width: int = 44) -> Card:
        outer_width = width
        width = max(12, width - 4)
        bar_width = max(2, min(bar_width, width - 25))
        card = Table.grid()
        card.add_column(width=width)

        title = Text(no_wrap=True)
        title.append(f"GPU {device.index}", style="bold")
        # The model is identical across a homogeneous box; truncating it is fine.
        title.append(f"  {device.name}"[: width - 8], style="grey50")
        card.add_row(title)

        util = None if device.util_gpu is None else device.util_gpu / 100.0
        # "100%" rather than "100.0%": the card is narrow and the integer is
        # enough resolution to judge utilisation at a glance.
        util_pct = "-" if util is None else f"{device.util_gpu}%"
        mem_fraction = (
            None
            if not device.mem_total or device.mem_used is None
            else device.mem_used / device.mem_total
        )

        value_width = max(8, width - bar_width - 7)

        inner = Table.grid(padding=(0, 1))
        inner.add_column(no_wrap=True, width=5, justify="right")
        inner.add_column(no_wrap=True, width=bar_width)
        inner.add_column(no_wrap=True, width=value_width, justify="left")
        inner.add_row(
            Text("util", style="grey58"),
            bar(util, bar_width),
            Text(util_pct, style=heat(util) if util is not None else "grey42"),
        )
        inner.add_row(
            Text("mem", style="grey58"),
            bar(mem_fraction, bar_width, warn=0.7, crit=0.92, style="cyan"),
            Text(
                f"{human_bytes(device.mem_used)} / {human_bytes(device.mem_total)}",
                style="cyan",
            ),
        )

        facts = Table.grid(padding=(0, 1))
        facts.add_column(no_wrap=True, width=5, justify="right")
        facts.add_column(
            no_wrap=True, width=value_width + bar_width + 1, justify="left"
        )
        # These helpers already return styled Text; do not wrap them again.
        facts.add_row(Text("temp", style="grey58"), self._temp(device))
        facts.add_row(Text("power", style="grey58"), self._power(device))
        facts.add_row(Text("fan", style="grey58"), self._fan(device))
        facts.add_row(
            Text("clock", style="grey58"),
            Text(
                f"{device.clock_mhz if device.clock_mhz is not None else '-'} / {device.mem_clock_mhz if device.mem_clock_mhz is not None else '-'} MHz"
            ),
        )

        card.add_row(inner)
        card.add_row(facts)
        return Card(card, width=outer_width, padding=(0, 1), border_style="grey30")

    def _temp(self, device: GPUDevice) -> Text:
        if device.temperature is None:
            return Text("-", style="grey50")
        return Text(
            f"{device.temperature}°C",
            style="bold red"
            if device.temperature >= self.settings.thresholds.gpu_hot
            else "yellow"
            if device.temperature >= self.settings.thresholds.gpu_warm
            else "green",
        )

    @staticmethod
    def _power(device: GPUDevice) -> str:
        if device.power_w is None:
            return Text("-", style="grey50")
        limit = device.power_limit_w
        suffix = f" / {limit:.0f}W" if limit else ""
        fraction = (device.power_w / limit) if limit else None
        return Text(
            f"{device.power_w:.1f}W{suffix}",
            style=heat(fraction, warn=0.75, crit=0.92) if fraction else "white",
        )

    @staticmethod
    def _fan(device: GPUDevice) -> str:
        if device.fan is None:
            return Text("-", style="grey50")
        return Text(
            f"{device.fan}%", style=heat(device.fan / 100.0, warn=0.7, crit=0.9)
        )

    def _note(self, snapshot: GPUSnapshot) -> Text:
        if not snapshot.devices:
            return Text("")
        if snapshot.error:
            return Text(snapshot.error, style="grey62")
        note = Text()
        note.append("processes on gpu", style="bold grey58")
        note.append("  ·  ", style="grey35")
        note.append(f"{len(snapshot.processes)} total", style="grey62")
        if self.compute_only:
            note.append("  ·  ", style="grey35")
            note.append("compute only (g)", style="yellow")
        if snapshot.source == "pmon":
            # Worth saying out loud, because the numbers came from a subprocess
            # rather than NVML, which behaves differently in terms of freshness.
            note.append("  ·  ", style="grey35")
            note.append("per-process via nvidia-smi pmon", style="grey42")
        return note

    # -- processes ---------------------------------------------------------

    def _visible(self, snapshot: GPUSnapshot):
        if not self.compute_only:
            return snapshot.processes
        return [proc for proc in snapshot.processes if "C" in proc.kind]

    def _order(self, snapshot: GPUSnapshot) -> list[tuple[int, int]]:
        # Heaviest GPU memory consumer first: that is what you are usually hunting.
        visible = self._visible(snapshot)
        return [
            (proc.pid, proc.gpu_index)
            for proc in sorted(
                visible,
                key=lambda proc: (-(proc.mem_bytes or 0), proc.pid, proc.gpu_index),
            )
        ]

    def _rows(self, snapshot: GPUSnapshot) -> dict[tuple[int, int], tuple]:
        rows: dict[tuple[int, int], tuple] = {}
        for proc in self._visible(snapshot):
            info = snapshot.details.get(proc.pid)
            # One process can hold memory on several GPUs (Xorg does on every
            # display), so a row is identified by the (pid, gpu) pair, not the pid.
            rows[(proc.pid, proc.gpu_index)] = (
                str(proc.pid),
                fmt_timestamp(info.start_ts) if info else "-",
                info.username if info else "-",
                str(proc.gpu_index),
                Text(proc.kind, style=TYPE_STYLES.get(proc.kind, "grey58")),
                pct(proc.sm_pct, digits=0, width=5)
                if proc.sm_pct is not None
                else Text("-", style="grey50"),
                pct(proc.mem_pct, digits=0, width=5)
                if proc.mem_pct is not None
                else Text("-", style="grey50"),
                human_bytes(proc.mem_bytes),
                human_duration(info.age) if info else "-",
                Text(info.cmdline if info else "[exited or inaccessible]"),
            )
        return rows

    # -- interaction -------------------------------------------------------

    def toggle_kind(self) -> None:
        self.compute_only = not self.compute_only
        if self._sync is not None:
            self._sync.invalidate()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # The GPU process table is ordered by VRAM; ignore header clicks.
        event.stop()

    def selected_process(self):
        table = self.query_one("#gpu-procs", DataTable)
        if not table.row_count or self._latest is None:
            return None
        pid = int(str(table.get_row_at(table.cursor_coordinate.row)[0]))
        return self._latest.details.get(pid)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.action_process_details()
