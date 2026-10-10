"""CPU panel: aggregate and per-core utilisation."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widgets import Static
from textual.containers import VerticalScroll

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import bar, fmt_clock, heat
from ..sources.cpu import CPUStats

# Sparkline history of the aggregate, so short spikes stay visible.
HISTORY = 60


class CPUPanel(Panel):
    KEY = "1"
    TITLE = "CPU"
    INTERVAL = 1.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = SOURCES.cpu
        self.history: list[float] = []
        self._latest: CPUStats | None = None

    def body(self):
        yield Static(id="cpu-header")
        yield Static(id="cpu-aggregate")
        with VerticalScroll(id="cpu-scroll"):
            yield Static(id="cpu-cores")

    def fetch(self) -> CPUStats:
        return self.source.sample()

    def apply(self, stats: CPUStats) -> None:
        self._latest = stats
        self.history.append(stats.total)
        del self.history[:-HISTORY]
        self.query_one("#cpu-header", Static).update(self._header(stats))
        self.query_one("#cpu-aggregate", Static).update(self._aggregate(stats))
        self.query_one("#cpu-cores", Static).update(self._cores(stats))

    def on_resize(self) -> None:
        if self._latest is not None and self.is_mounted:
            stats = self._latest
            self.query_one("#cpu-header", Static).update(self._header(stats))
            self.query_one("#cpu-aggregate", Static).update(self._aggregate(stats))
            self.query_one("#cpu-cores", Static).update(self._cores(stats))

    def status_prefix(self) -> str:
        return ""

    # -- rendering ---------------------------------------------------------

    def _sparkline(self, values: list[float], width: int) -> Text:
        """Aggregate utilisation history, most recent sample on the right.

        The trail is drawn right-aligned against a fixed-width label so that a
        slowly-growing history visibly *climbs* the line from the right instead of
        appearing to sit still. A one-cell gap marks "now".
        """
        levels = "▁▂▃▄▅▆▇█"
        span = max(1, width - 1)
        shown = values[-span:]
        glyphs = "".join(
            levels[min(len(levels) - 1, int(max(0.0, min(1.0, v)) * len(levels)))]
            for v in shown
        )
        # Pad on the left: absent history is blank, and real samples hug "now".
        text = Text(no_wrap=True)
        text.append(" " * (span - len(glyphs)), style="grey30")
        for index, glyph in enumerate(glyphs):
            value = shown[index]
            text.append(glyph, style=heat(max(0.0, min(1.0, value))))
        text.append(" ", style="grey30")
        return text

    def _header(self, stats: CPUStats) -> Text:
        # Core count and clock right-aligned so the layout does not shift when
        # the frequency changes width.
        available = max(1, (self.size.width or 100) - 4)
        model = stats.model
        meta = f"{stats.online_cores} cores"
        if stats.freq_mhz:
            meta += f"  ·  {stats.freq_mhz:,.0f} MHz"
        clock = fmt_clock()
        room = available - len(model) - len(meta) - len(clock) - 8
        if room < 12:
            # Too narrow for the full model string; drop it to the tail.
            model = model[: max(8, room)] if room > 8 else ""
            meta = f"{stats.online_cores} cores"

        text = Text(no_wrap=True)
        if model:
            text.append(model, style="bold")
            text.append("  ·  ", style="grey35")
        text.append(meta, style="grey62")
        padding = max(1, available - len(text.plain) - len(clock))
        text.append(" " * padding)
        text.append(clock, style="grey42")
        return text

    def _aggregate(self, stats: CPUStats) -> Table:
        # One wide bar spanning the panel reads better than a short fixed one.
        width = max(20, min(80, (self.size.width or 100) - 30))

        bar_row = Table.grid(padding=(0, 2))
        bar_row.add_column(no_wrap=True, width=8, justify="right")
        bar_row.add_column(no_wrap=True, width=width)
        bar_row.add_column(no_wrap=True, width=6, justify="left")
        bar_row.add_row(
            Text("host", style="bold"),
            bar(stats.total, width),
            Text(f"{stats.total * 100:.0f}%", style=f"bold {heat(stats.total)}"),
        )

        # Load average sits alongside the component split on one line, so the summary
        # block is four lines rather than six and the core grid starts higher up.
        load = Table.grid(padding=(0, 1))
        load.add_column(width=8, no_wrap=True)
        load.add_column(no_wrap=True, justify="left")
        load.add_column(no_wrap=True, justify="left")
        load.add_column(no_wrap=True, justify="left")
        load.add_row(
            Text(""),
            Text(f"user {stats.user * 100:.0f}%", style="green"),
            Text(f"sys {stats.system * 100:.0f}%", style="yellow"),
            Text(f"iowait {stats.iowait * 100:.0f}%", style="magenta"),
        )
        # steal is rare on a workstation; only show it when non-zero.
        if stats.steal > 0.0005:
            load.add_row(Text(""), Text(f"steal {stats.steal * 100:.0f}%", style="red"))
        load.add_row(
            Text("load"),
            Text(f"{stats.load1:.2f}", style="bold"),
            Text(f"{stats.load5:.2f}", style="grey62"),
            Text(f"{stats.load15:.2f}", style="grey62"),
        )

        outer = Table.grid(padding=(0, 1))
        outer.add_column()
        outer.add_row(bar_row)
        outer.add_row(load)
        if self.history:
            trend = Table.grid(padding=(0, 1))
            trend.add_column(width=8, no_wrap=True)
            trend.add_column(no_wrap=True)
            trend.add_row(
                Text("history", style="grey58"),
                self._sparkline(self.history, width),
            )
            outer.add_row(trend)
        return outer

    def _cores(self, stats: CPUStats) -> Table:
        cores = stats.cores or [("cpu0", 0.0)]
        available = max(1, self.size.width or 80)
        label_width = max(5, max(len(label) for label, _ in cores))
        # "cpu12 " + bar + " 100" is about 30 columns including padding.
        per_row = max(1, min(8, (available - 2) // (label_width + 25)))
        bar_width = max(6, min(20, (available // per_row) - label_width - 7))

        grid = Table.grid(padding=(0, 2))
        for _ in range(per_row):
            grid.add_column(no_wrap=True)
        grid.add_row(Text("per-core utilisation", style="bold grey58"))

        for start in range(0, len(cores), per_row):
            row: list[object] = []
            for label, usage in cores[start : start + per_row]:
                cell = Table.grid(padding=(0, 1))
                cell.add_column(no_wrap=True, width=label_width, justify="right")
                cell.add_column(no_wrap=True, width=bar_width)
                cell.add_column(no_wrap=True, width=4, justify="right")
                cell.add_row(
                    Text(label, style="grey58"),
                    bar(usage, bar_width),
                    Text(f"{usage * 100:.0f}%"),
                )
                row.append(cell)
            while len(row) < per_row:
                row.append(Text(""))
            grid.add_row(*row)
        return grid
