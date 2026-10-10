"""A resource-first overview, with bounded cards and recent trends."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich.panel import Panel as Card
from rich.table import Table
from rich.text import Text
from textual.widgets import Static
from textual.containers import VerticalScroll

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import (
    bar_cells,
    fmt_clock,
    human_bytes,
    human_rate,
    sparkline,
)
from ..sources.summary import (
    SystemSummary,
    busiest_interfaces,
    build_alerts,
    top_by_cpu,
    top_by_memory,
)

MEDIUM = 110
WIDE = 146
CARD_HEIGHT = 8  # Six content rows and two borders, independent of live values.
SEVERITY = {1: ("▲", "bold red"), 2: ("•", "yellow"), 3: ("·", "grey62")}


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    summary: SystemSummary


class OverviewPanel(Panel):
    KEY = "0"
    TITLE = "Overview"
    INTERVAL = 1.5
    _SECTIONS = ("cpu", "memory", "gpu", "network", "top cpu", "top memory")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cpu, self.mem, self.gpu = SOURCES.cpu, SOURCES.mem, SOURCES.gpu
        self.net, self.proc = SOURCES.net, SOURCES.proc
        self.last_summary: SystemSummary | None = None
        self.cpu_history: list[float] = []
        self.memory_history: list[float] = []

    def body(self):
        with VerticalScroll(id="overview-scroll"):
            yield Static("Collecting system counters…", id="overview-body")

    def fetch(self) -> OverviewSnapshot:
        cpu, memory = self.cpu.sample(), self.mem.sample()
        processes, gpu, net = self.proc.sample(), self.gpu.sample(), self.net.sample()
        net = replace(
            net,
            ifaces=tuple(
                iface
                for iface in net.ifaces
                if iface.name not in self.settings.hidden_interfaces
            ),
        )
        resources, disk = SOURCES.resources.sample(), SOURCES.disk.sample()
        alerts = build_alerts(cpu, memory, gpu, net, self.settings.thresholds)
        return OverviewSnapshot(
            SystemSummary(
                cpu=cpu,
                memory=memory,
                gpu=gpu,
                net=net,
                top_cpu=tuple(top_by_cpu(processes)),
                top_memory=tuple(top_by_memory(processes)),
                gpu_processes=(),
                busy_interfaces=tuple(busiest_interfaces(net)),
                alerts=alerts,
                alert_severity=alerts[0].severity if alerts else 0,
                process_count=len(processes),
                disk=disk,
                resources=resources,
            )
        )

    def apply(self, data: OverviewSnapshot) -> None:
        self.last_summary = data.summary
        self.cpu_history.append(data.summary.cpu.total)
        mem = data.summary.memory
        self.memory_history.append(mem.used / mem.total if mem.total else 0)
        del self.cpu_history[:-40]
        del self.memory_history[:-40]
        self._redraw()

    def on_resize(self) -> None:
        self.call_after_refresh(self._redraw)

    def _redraw(self) -> None:
        if self.last_summary is not None and self.is_mounted:
            body = self.query_one("#overview-body", Static)
            body.update(self._view(self.last_summary, body.content_size.width or None))

    def _render_section(self, builder, label: str, width: int = 46) -> str:
        from rich.console import Console

        if self.last_summary is None:
            return ""
        console = Console(width=width, no_color=True)
        with console.capture() as capture:
            console.print(builder(self.last_summary, width))
        return capture.get().strip()

    def section_texts(self) -> dict[str, str]:
        return {
            name: self._render_section(
                getattr(self, f"_{name.replace(' ', '_')}"), name
            )
            for name in self._SECTIONS
        }

    def _columns(self, width: int) -> int:
        return 3 if width >= WIDE else 2 if width >= MEDIUM else 1

    @staticmethod
    def _text(value: str, width: int, style: str = "") -> Text:
        text = Text(value, style=style, no_wrap=True)
        text.truncate(max(1, width), overflow="ellipsis")
        return text

    def _card(self, content, width: int, height: int = CARD_HEIGHT) -> Card:
        return Card(
            content, width=width, height=height, padding=(0, 1), border_style="grey30"
        )

    def _row(self, summary, names: tuple[str, ...], width: int) -> Table:
        share = (width - 2 * (len(names) - 1)) // len(names)
        grid = Table.grid(padding=(0, 2))
        for _ in names:
            grid.add_column(width=share)
        grid.add_row(
            *(
                self._card(
                    getattr(self, f"_{name.replace(' ', '_')}")(summary, share - 4),
                    share,
                )
                for name in names
            )
        )
        return grid

    def _view(self, summary: SystemSummary, width: int | None = None) -> Table:
        width = max(30, width if width is not None else (self.size.width or 100) - 4)
        columns = self._columns(width)
        outer = Table.grid()
        outer.add_column(width=width)
        headline = (
            f"{len(summary.alerts)} threshold notice(s) · [n] all notices"
            if summary.alerts
            else "No thresholds exceeded"
        )
        outer.add_row(
            self._text(
                f"{headline}  ·  {summary.process_count} visible processes  ·  {fmt_clock(summary.collected_at)}",
                width,
                SEVERITY.get(summary.alert_severity, ("", "grey62"))[1],
            )
        )
        # Reserve two notice rows so thresholds crossing never shift the cards.
        for index in range(2):
            if index == 1 and len(summary.alerts) > 2:
                outer.add_row(
                    self._text(
                        f"{len(summary.alerts) - 1} more notices · [n] opens all",
                        width,
                        "yellow",
                    )
                )
            elif index < len(summary.alerts):
                alert = summary.alerts[index]
                glyph, style = SEVERITY[alert.severity]
                outer.add_row(self._text(f"{glyph} {alert.message}", width, style))
            else:
                outer.add_row(Text(""))
        if columns == 3:
            outer.add_row(self._row(summary, ("cpu", "memory", "gpu"), width))
            outer.add_row(self._row(summary, ("network",), width))
        elif columns == 2:
            outer.add_row(self._row(summary, ("cpu", "memory"), width))
            outer.add_row(self._row(summary, ("gpu", "network"), width))
        else:
            resources = Table.grid()
            resources.add_column(width=width - 4)
            resources.add_row(self._cpu(summary, width - 4))
            resources.add_row(Text(""))
            resources.add_row(self._memory(summary, width - 4))
            outer.add_row(self._card(resources, width, height=14))
            outer.add_row(
                self._row(
                    summary, ("gpu", "network") if width >= 70 else ("gpu",), width
                )
            )
            if width < 70:
                outer.add_row(self._row(summary, ("network",), width))
        outer.add_row(
            self._row(
                summary,
                ("top cpu", "top memory") if width >= 70 else ("top cpu",),
                width,
            )
        )
        if width < 70:
            outer.add_row(self._row(summary, ("top memory",), width))
        outer.add_row(
            self._row(
                summary, ("disk", "resources") if width >= 70 else ("disk",), width
            )
        )
        if width < 70:
            outer.add_row(self._row(summary, ("resources",), width))
        return outer

    def _cpu(self, summary, width: int) -> Table:
        stats = summary.cpu
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(self._text(f"host cpu · {stats.total:.1%} busy", width, "bold"))
        grid.add_row(bar_cells(stats.total, max(1, width)))
        grid.add_row(
            self._text(
                f"User {stats.user:.1%}  system {stats.system:.1%}  wait {stats.iowait:.1%}",
                width,
                "grey62",
            )
        )
        grid.add_row(
            self._text(
                f"{stats.online_cores} CPUs · load 1/5/15m {stats.load1:.2f}/{stats.load5:.2f}/{stats.load15:.2f}",
                width,
                "grey62",
            )
        )
        if self.cpu_history:
            grid.add_row(
                Text.assemble(
                    ("0–100% ", "grey50"),
                    sparkline(self.cpu_history, max(1, width - 7)),
                )
            )
        else:
            grid.add_row(Text(""))
        return grid

    def _memory(self, summary, width: int) -> Table:
        stats = summary.memory
        fraction = stats.used / stats.total if stats.total else None
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(
            self._text(
                f"host memory · {'n/a' if fraction is None else f'{fraction:.1%}'} used",
                width,
                "bold",
            )
        )
        grid.add_row(bar_cells(fraction, max(1, width)))
        grid.add_row(
            self._text(
                f"{human_bytes(stats.used)} / {human_bytes(stats.total)} · {human_bytes(stats.available)} available",
                width,
                "grey62",
            )
        )
        if stats.available_estimated:
            grid.add_row(
                self._text("Availability estimated (older kernel)", width, "grey50")
            )
        else:
            grid.add_row(Text(""))
        grid.add_row(
            self._text(
                f"Swap {human_bytes(stats.swap_used)} / {human_bytes(stats.swap_total)}"
                if stats.swap_total
                else "Swap not configured",
                width,
                "grey62",
            )
        )
        if self.memory_history:
            grid.add_row(
                Text.assemble(
                    ("0–100% ", "grey50"),
                    sparkline(self.memory_history, max(1, width - 7)),
                )
            )
        else:
            grid.add_row(Text(""))
        return grid

    def _gpu(self, summary, width: int) -> Table:
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(self._text("gpu · [4] details", width, "bold"))
        if not summary.gpu.devices:
            grid.add_row(
                self._text(
                    summary.gpu.error or "No supported GPU telemetry", width, "grey50"
                )
            )
            return grid
        rows = Table.grid(padding=(0, 1))
        for cells in (2, 6, 4, 5, max(1, width - 21)):
            rows.add_column(width=cells, no_wrap=True)
        grid.add_row(self._text("ID  Busy (0–100%)  Temp  VRAM", width, "grey62"))
        devices = summary.gpu.devices
        shown = devices[:3] if len(devices) > 4 else devices
        for device in shown:
            util = None if device.util_gpu is None else device.util_gpu / 100
            memory = human_bytes(device.mem_used)
            if width >= 40:
                memory += f" / {human_bytes(device.mem_total)}"
            rows.add_row(
                Text(str(device.index)),
                bar_cells(util, 6),
                Text("n/a" if util is None else f"{util:.0%}"),
                Text("-" if device.temperature is None else f"{device.temperature}°C"),
                self._text(memory, max(1, width - 21), "cyan"),
            )
        grid.add_row(rows)
        if len(devices) > 4:
            grid.add_row(
                self._text(
                    f"{len(devices) - len(shown)} more GPUs · [4] details",
                    width,
                    "grey62",
                )
            )
        return grid

    def _network(self, summary, width: int) -> Table:
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(self._text("network · [5] details", width, "bold"))
        if summary.net.error:
            grid.add_row(self._text(summary.net.error, width, "grey50"))
        if not summary.busy_interfaces:
            grid.add_row(
                self._text("No non-loopback traffic in this sample", width, "grey50")
            )
        for iface in summary.busy_interfaces[:4]:
            grid.add_row(
                self._text(
                    f"{iface.name}  ↓ {human_rate(iface.rx_rate)}  ↑ {human_rate(iface.tx_rate)}",
                    width,
                )
            )
        return grid

    def _processes(self, summary, width: int, memory: bool) -> Table:
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(
            self._text(
                "top memory · RSS" if memory else "top cpu · 100% = one CPU",
                width,
                "bold",
            )
        )
        items = summary.top_memory if memory else summary.top_cpu
        if not items:
            grid.add_row(
                self._text(
                    "No processes above 64 MiB" if memory else "No processes above 1%",
                    width,
                    "grey50",
                )
            )
        for item in items[:5]:
            proc = item.info
            value = human_bytes(proc.rss_bytes) if memory else f"{proc.cpu_pct:.1f}%"
            grid.add_row(self._text(f"{value:>9}  {proc.pid:>7}  {proc.name}", width))
        return grid

    def _top_cpu(self, summary, width: int) -> Table:
        return self._processes(summary, width, False)

    def _top_memory(self, summary, width: int) -> Table:
        return self._processes(summary, width, True)

    def _disk(self, summary, width: int) -> Table:
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(self._text("disk · [6] activity and capacity", width, "bold"))
        if summary.disk is None:
            grid.add_row(self._text("Disk counters unavailable", width, "grey50"))
            return grid
        if summary.disk.error:
            grid.add_row(self._text(summary.disk.error, width))
        devices = [
            disk
            for disk in summary.disk.devices
            if not disk.partition and not disk.name.startswith(("loop", "ram"))
        ]
        devices.sort(key=lambda disk: -(disk.read_rate or 0) - (disk.write_rate or 0))
        for disk in devices[:3]:
            grid.add_row(
                self._text(
                    f"{disk.name}  R {human_rate(disk.read_rate)}  W {human_rate(disk.write_rate)}",
                    width,
                )
            )
        return grid

    def _resources(self, summary, width: int) -> Table:
        grid = Table.grid()
        grid.add_column(width=width)
        grid.add_row(self._text("pressure / cgroup · [7] details", width, "bold"))
        if summary.resources is None:
            grid.add_row(self._text("Resource counters unavailable", width, "grey50"))
            return grid
        for pressure in summary.resources.pressure[:3]:
            value = f"{pressure.some.avg10:.2f}%" if pressure.some else "unavailable"
            grid.add_row(
                self._text(f"{pressure.resource} some stalls (10s): {value}", width)
            )
        group = summary.resources.cgroup
        percent = "-" if group.cpu_pct is None else f"{group.cpu_pct:.1f}%"
        capacity = (
            "-" if group.cpu_capacity is None else f"{group.cpu_capacity:.2f} CPUs"
        )
        grid.add_row(self._text(f"Cgroup CPU: {percent} of {capacity}", width))
        grid.add_row(
            self._text(
                f"Cgroup RAM: {human_bytes(group.memory_used)} / {human_bytes(group.memory_capacity)}",
                width,
            )
        )
        return grid
