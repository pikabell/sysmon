"""Memory pressure and kernel counters, without overlapping stacked segments."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widgets import Static
from textual.containers import VerticalScroll

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import bar_cells, human_bytes, heat
from ..sources.memory import MemStats


class MemoryPanel(Panel):
    KEY = "2"
    TITLE = "Memory"
    INTERVAL = 1.5

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = SOURCES.mem
        self._latest: MemStats | None = None

    def body(self):
        with VerticalScroll(id="mem-scroll"):
            yield Static(id="mem-body")

    def fetch(self) -> MemStats:
        return self.source.sample()

    def apply(self, stats: MemStats) -> None:
        self._latest = stats
        self.query_one("#mem-body", Static).update(self._view(stats))

    def on_resize(self) -> None:
        if self._latest is not None and self.is_mounted:
            self.query_one("#mem-body", Static).update(self._view(self._latest))

    def _view(self, stats: MemStats, width: int | None = None) -> Table:
        width = max(24, width if width is not None else (self.size.width or 100) - 6)
        outer = Table.grid()
        outer.add_column(width=width)
        fraction = stats.used / stats.total if stats.total else 0
        outer.add_row(
            Text(f"Host RAM  {fraction:.1%} used", style="bold " + heat(fraction))
        )
        outer.add_row(bar_cells(fraction, min(70, width)))
        outer.add_row(
            Text(f"{human_bytes(stats.used)} used / {human_bytes(stats.total)} total")
        )
        estimate = " (estimated)" if stats.available_estimated else ""
        outer.add_row(
            Text(
                f"{human_bytes(stats.available)} available for new work{estimate}",
                style="green",
            )
        )
        outer.add_row(
            Text(
                "Used = total − available; cache is partly reclaimable.", style="grey62"
            )
        )
        outer.add_row(Text(""))
        if stats.swap_total:
            fraction = stats.swap_used / stats.swap_total
            outer.add_row(Text(f"Swap  {fraction:.1%} occupied", style="bold"))
            outer.add_row(bar_cells(fraction, min(70, width), warn=0.5, crit=0.9))
            outer.add_row(
                Text(
                    f"{human_bytes(stats.swap_used)} / {human_bytes(stats.swap_total)}"
                )
            )
        else:
            outer.add_row(Text("Swap not configured", style="grey50"))
        outer.add_row(Text(""))
        outer.add_row(
            Text("Kernel counters (overlap; do not add together)", style="bold")
        )
        facts = Table.grid(padding=(0, 2))
        facts.add_column(width=12, style="grey62")
        facts.add_column()
        for label, value in (
            ("free", stats.free),
            ("cache", stats.cached),
            ("buffers", stats.buffers),
            ("slab", stats.slab),
            ("dirty", stats.dirty),
        ):
            facts.add_row(label, human_bytes(value))
        outer.add_row(facts)
        return outer
