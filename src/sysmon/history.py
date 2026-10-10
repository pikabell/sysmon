"""Bounded session history. Only collected samples are retained; gaps stay gaps."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from rich.text import Text

from .core.render import human_rate


@dataclass(frozen=True, slots=True)
class Point:
    timestamp: float
    monotonic: float
    value: float | None


class History:
    def __init__(self, limit: int = 600):
        self.limit = limit
        self.series: dict[str, deque[Point]] = {}
        self.units: dict[str, str] = {}
        self._owners: list[str] = []
        self._panel_series: dict[str, set[str]] = {}
        self.hidden_interfaces: tuple[str, ...] = ()

    def add(self, name: str, value: float | None, unit: str = "%"):
        if name not in self.series and len(self.series) >= 256:
            return
        self.units[name] = unit
        queue = self.series.setdefault(name, deque(maxlen=self.limit))
        if queue.maxlen != self.limit:
            queue = self.series[name] = deque(queue, maxlen=self.limit)
        queue.append(Point(time.time(), time.monotonic(), value))
        for owner in self._owners:
            self._panel_series.setdefault(owner, set()).add(name)

    def record(self, key: str, data):
        self._owners.append(key)
        try:
            self._record(key, data)
        finally:
            self._owners.pop()

    def failed(self, key: str):
        for name in sorted(self._panel_series.get(key, ())):
            self.add(name, None, self.units[name])

    def resize(self, limit: int):
        self.limit = limit
        self.series = {
            name: deque(values, maxlen=limit) for name, values in self.series.items()
        }

    def set_hidden(self, interfaces: tuple[str, ...]):
        self.hidden_interfaces = interfaces
        for interface in interfaces:
            for name in (f"{interface} RX", f"{interface} TX"):
                self.series.pop(name, None)
                self.units.pop(name, None)
                for names in self._panel_series.values():
                    names.discard(name)

    def _record(self, key: str, data):
        if key == "0":
            summary = data.summary
            self.record("1", summary.cpu)
            self.record("2", summary.memory)
            self.record("4", summary.gpu)
            self.record("5", summary.net)
            if summary.disk is not None:
                self.record("6", summary.disk)
            if summary.resources is not None:
                self.record("7", summary.resources)
        elif key == "1":
            self.add("Host CPU busy", data.total * 100)
            self.add("Host CPU iowait", data.iowait * 100)
        elif key == "2":
            self.add(
                "Host RAM used", data.used / data.total * 100 if data.total else None
            )
            self.add(
                "Swap used",
                data.swap_used / data.swap_total * 100 if data.swap_total else None,
            )
        elif key == "4":
            for device in data.devices:
                self.add(f"GPU {device.index} busy", device.util_gpu)
                self.add(
                    f"GPU {device.index} VRAM",
                    device.mem_used / device.mem_total * 100
                    if device.mem_used is not None and device.mem_total
                    else None,
                )
        elif key == "5":
            for iface in data.ifaces:
                if iface.name in self.hidden_interfaces:
                    continue
                self.add(f"{iface.name} RX", iface.rx_rate, "B/s")
                self.add(f"{iface.name} TX", iface.tx_rate, "B/s")
        elif key == "6":
            for disk in data.devices:
                self.add(f"{disk.name} read", disk.read_rate, "B/s")
                self.add(f"{disk.name} write", disk.write_rate, "B/s")
                self.add(
                    f"{disk.name} busy", None if disk.busy is None else disk.busy * 100
                )
        elif key == "7":
            for pressure in data.pressure:
                self.add(
                    f"System {pressure.resource} pressure (some, 10s)",
                    pressure.some.avg10 if pressure.some else None,
                )
            group = data.cgroup
            self.add("Cgroup CPU capacity used", group.cpu_pct)
            self.add(
                "Cgroup RAM capacity used",
                group.memory_used / group.memory_capacity * 100
                if group.memory_used is not None and group.memory_capacity
                else None,
            )


def plot(points: list[Point], unit: str, width: int = 65, height: int = 12) -> Text:
    if not points:
        return Text("No samples yet. Visit the panel that collects this metric.")
    shown = points[-max(1, width) :]
    values = [point.value for point in shown if point.value is not None]
    ceiling = 100.0 if unit == "%" else max(values, default=1) or 1
    lines = []
    for row in range(height, 0, -1):
        cutoff = ceiling * row / height
        label = f"{cutoff:6.1f}%" if unit == "%" else f"{human_rate(cutoff):>12}"
        cells = "".join(
            "?" if point.value is None else "█" if point.value >= cutoff else " "
            for point in shown
        )
        lines.append(f"{label} │{cells}")
    lines.append(" " * (8 if unit == "%" else 12) + " └" + "─" * len(shown))
    span = max(0.0, shown[-1].monotonic - shown[0].monotonic)
    lines.append(
        f"Oldest → newest · {len(shown)} samples over {span:.1f}s · ? = unavailable"
    )
    if values:
        fmt = (lambda value: f"{value:.2f}%") if unit == "%" else human_rate
        lines.append(
            f"Min {fmt(min(values))} · max {fmt(max(values))} · last {fmt(shown[-1].value) if shown[-1].value is not None else '-'}"
        )
    else:
        lines.append("All readings unavailable")
    lines.append(
        "Fixed 0–100% scale"
        if unit == "%"
        else "Rate scale adapts to the displayed peak"
    )
    return Text("\n".join(lines), style="cyan", no_wrap=True)
