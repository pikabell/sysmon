"""One combined sample for the overview.

The overview needs every subsystem at once. Taking them in sequence on the worker
thread costs about 70 ms warm (plus the GPU's cached pmon result), which is well
inside the overview's refresh interval.

The interesting part is what counts as *notable*. A raw top-N by CPU is mostly
idle kernels and a sleeping desktop, so a process is only surfaced when it is
actually consuming something, and the single largest consumer of CPU, memory and
GPU memory is called out even if it is quiet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .cpu import CPUStats
from .gpu import GPUSnapshot
from .memory import MemStats
from .net import NetSnapshot
from .procs import ProcInfo
from .disk import DiskSnapshot
from .resources import ResourceSnapshot
from ..config import Thresholds

# A process must clear these to be considered active.
MIN_CPU_PCT = 1.0
MIN_RSS_MIB = 64

# How many entries of each kind to surface.
TOP_PROCESSES = 5
TOP_GPU_PROCESSES = 4
TOP_INTERFACES = 4


@dataclass(frozen=True, slots=True)
class Notable:
    """A process worth surfacing, with why it was picked."""

    info: ProcInfo
    reason: str


@dataclass(frozen=True, slots=True)
class Alert:
    """A threshold breach worth reporting.

    Severity drives both ordering and the glyph shown next to it: 1 is critical
    (something is wrong now), 2 is a warning, 3 is a notice that is usually just
    load. Carrying it on the object means the panel never has to re-derive which
    alerts are serious.
    """

    severity: int
    message: str


@dataclass(frozen=True, slots=True)
class SystemSummary:
    cpu: CPUStats
    memory: MemStats
    gpu: GPUSnapshot
    net: NetSnapshot
    top_cpu: tuple[Notable, ...]
    top_memory: tuple[Notable, ...]
    gpu_processes: tuple[Notable, ...]
    busy_interfaces: tuple = ()
    #: Threshold breaches, ordered most severe first.
    alerts: tuple[Alert, ...] = ()
    #: Severity of the worst alert: 1 critical, 2 warning, 3 notice, 0 none.
    alert_severity: int = 0

    @property
    def alert_messages(self) -> tuple[str, ...]:
        return tuple(alert.message for alert in self.alerts)

    process_count: int = 0
    collected_at: float = field(default_factory=time.time)
    disk: DiskSnapshot | None = None
    resources: ResourceSnapshot | None = None


def top_by_cpu(table: dict[int, ProcInfo], limit: int = TOP_PROCESSES) -> list[Notable]:
    """Busiest processes, ignoring the idle and sleeping noise."""
    active = [
        Notable(info=info, reason=f"{info.cpu_pct:.0f}% cpu")
        for info in table.values()
        if info.cpu_pct >= MIN_CPU_PCT and info.cmdline
    ]
    active.sort(key=lambda item: -item.info.cpu_pct)
    return active[:limit]


def top_by_memory(
    table: dict[int, ProcInfo], limit: int = TOP_PROCESSES
) -> list[Notable]:
    resident = [
        Notable(info=info, reason=human_mib(info.rss_bytes))
        for info in table.values()
        if info.rss_bytes >= MIN_RSS_MIB * 2**20 and info.cmdline
    ]
    resident.sort(key=lambda item: -item.info.rss_bytes)
    return resident[:limit]


def human_mib(value: int) -> str:
    return f"{value / 2**20:.0f}M"


def build_alerts(
    cpu: CPUStats,
    memory: MemStats,
    gpu: GPUSnapshot,
    net: NetSnapshot,
    thresholds: Thresholds | None = None,
) -> tuple[Alert, ...]:
    """Threshold breaches, most severe first.

    Deliberately conservative: only things that usually mean something is wrong, so
    the list stays empty on a healthy machine instead of always showing noise.
    """
    alerts: list[tuple[int, str]] = []
    thresholds = thresholds or Thresholds()

    # Current busy sample corroborated by the one-minute load average.
    if cpu.total * 100 > thresholds.cpu_busy and cpu.load1 > cpu.online_cores:
        alerts.append(
            (
                1,
                f"cpu saturated: {cpu.total * 100:.0f}% across {cpu.online_cores} cores, load {cpu.load1:.1f}",
            )
        )
    if cpu.iowait > 0.25:
        alerts.append(
            (2, f"high iowait: {cpu.iowait * 100:.0f}% reported (approximate)")
        )

    # Memory pressure.
    available_fraction = memory.available / memory.total if memory.total else 1
    if available_fraction * 100 < thresholds.memory_available:
        alerts.append(
            (1, f"memory low: only {memory.available / 2**30:.1f} GiB available")
        )
    if memory.swap_total and memory.swap_used / memory.swap_total > 0.5:
        alerts.append(
            (
                2,
                f"swap in use: {memory.swap_used / 2**30:.1f} GiB of {memory.swap_total / 2**30:.0f} GiB",
            )
        )

    # Monitor thresholds, not vendor throttling limits (which vary by hardware).
    for device in gpu.devices:
        if device.temperature is not None and device.temperature >= thresholds.gpu_hot:
            alerts.append((1, f"gpu {device.index} hot: {device.temperature}°C"))
        elif (
            device.temperature is not None and device.temperature >= thresholds.gpu_warm
        ):
            alerts.append((3, f"gpu {device.index} warm: {device.temperature}°C"))
        if device.mem_total and device.mem_used:
            if device.mem_used / device.mem_total > 0.95:
                alerts.append(
                    (
                        2,
                        f"gpu {device.index} vram nearly full: {device.mem_used / 2**30:.1f}/{device.mem_total / 2**30:.0f} GiB",
                    )
                )

    # Interface errors mean a real problem: dropped frames, bad cable.
    for iface in net.ifaces:
        errors = iface.rx_error_delta + iface.tx_error_delta
        if errors:
            alerts.append(
                (2, f"{iface.name}: {errors} new packet errors since last sample")
            )

    # Full GPU utilisation alone is normal useful work, not a health notice.
    alerts.sort(key=lambda item: item[0])
    return tuple(
        Alert(severity=severity, message=message) for severity, message in alerts
    )


def busiest_interfaces(net: NetSnapshot, limit: int = TOP_INTERFACES) -> list:
    """Interfaces that are actually moving data right now.

    A container's veth pairs and a down physical port sit at 0 B/s forever, so
    ranking by *current* rate keeps the overview's four slots meaningful. Ties are
    broken by lifetime traffic so an interface that was busy a moment ago still
    shows up on an otherwise idle box.
    """
    scored = [
        iface
        for iface in net.ifaces
        if iface.name != "lo" and (iface.rx_rate or iface.tx_rate)
    ]
    scored.sort(
        key=lambda iface: (
            -(iface.rx_rate + iface.tx_rate),
            -(iface.rx_bytes + iface.tx_bytes),
        )
    )
    return scored[:limit]
