"""Pressure and quota-aware accounting for the monitor's cgroup."""

from rich.table import Table
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import bar_cells, human_bytes


def pressure_table(pressures) -> Table:
    table = Table(expand=True, box=None, padding=(0, 1))
    for title in ("RESOURCE", "STALL", "10s%", "60s%", "300s%"):
        table.add_column(title)
    for pressure in pressures:
        if pressure.error:
            table.add_row(pressure.resource, Text(pressure.error), "-", "-", "-")
            continue
        for kind, line in (("some", pressure.some), ("full", pressure.full)):
            if line is not None:
                table.add_row(
                    pressure.resource,
                    kind,
                    f"{line.avg10:.2f}",
                    f"{line.avg60:.2f}",
                    f"{line.avg300:.2f}",
                )
    return table


class ResourcesPanel(Panel):
    KEY = "7"
    TITLE = "Resources"

    def body(self):
        with VerticalScroll(id="resources-scroll"):
            yield Static(
                "Collecting pressure and cgroup counters…", id="resources-body"
            )

    def fetch(self):
        return SOURCES.resources.sample()

    def apply(self, snapshot):
        self._latest = snapshot
        self._redraw()

    def on_resize(self):
        self._redraw()

    def _redraw(self):
        if not hasattr(self, "_latest") or not self.is_mounted:
            return
        snapshot = self._latest
        grid = Table.grid(padding=(1, 0))
        grid.add_column(width=max(25, self.size.width - 4))
        if snapshot.uptime_error:
            grid.add_row(Text(snapshot.uptime_error))
        grid.add_row(
            Text(
                "System pressure · time stalled", style="bold"
            )
        )
        grid.add_row(pressure_table(snapshot.pressure))
        grid.add_row(
            Text(
                "some: at least one task stalled · full: all non-idle tasks stalled\nCPU full is undefined system-wide. Missing readings remain unavailable.",
                style="grey62",
            )
        )
        group = snapshot.cgroup
        grid.add_row(
            Text(
                f"Monitor's cgroup · {group.version or 'unavailable'} · {group.path or '-'}",
                style="bold",
            )
        )
        capacity = (
            "-" if group.cpu_capacity is None else f"{group.cpu_capacity:.2f} CPUs"
        )
        percent = "-" if group.cpu_pct is None else f"{group.cpu_pct:.1f}%"
        grid.add_row(
            Text(
                f"CPU: {percent} of {capacity} (visible ancestor quotas + affinity/cpuset)"
            )
        )
        grid.add_row(
            bar_cells(
                None if group.cpu_pct is None else group.cpu_pct / 100,
                min(60, max(8, self.size.width - 6)),
            )
        )
        grid.add_row(
            Text(
                f"Memory: {human_bytes(group.memory_used)} / {human_bytes(group.memory_capacity)} effective capacity"
            )
        )
        fraction = (
            group.memory_used / group.memory_capacity
            if group.memory_used is not None and group.memory_capacity
            else None
        )
        grid.add_row(bar_cells(fraction, min(60, max(8, self.size.width - 6))))
        grid.add_row(
            Text(
                f"Visible hard memory limit: {human_bytes(group.memory_limit) if group.memory_limit is not None else 'none exposed'}",
                style="grey62",
            )
        )
        grid.add_row(
            Text(
                "Scope: this cgroup and descendants. Capacity includes visible ancestor limits.\nHost CPU and RAM readings are shown in panels 1 and 2.",
                style="grey62",
            )
        )
        for error in group.errors:
            grid.add_row(Text(error, style="yellow"))
        if group.pressure:
            grid.add_row(Text("Cgroup pressure", style="bold"))
            grid.add_row(pressure_table(group.pressure))
        self.query_one("#resources-body", Static).update(grid)
