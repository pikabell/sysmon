"""GPU telemetry: NVML per device, plus per-process usage.

Two tiers, because drivers differ in what they expose:

1. **Per-device** telemetry (utilisation, VRAM, temperature, power, fan, clocks)
   comes from NVML over ctypes and is fast enough to poll every tick.
2. **Per-process** usage prefers NVML's process queries, but plenty of drivers
   answer ``NVML_ERROR_NOT_SUPPORTED`` for them. When that happens the query is
   recorded as unsupported and this module falls back to parsing
   ``nvidia-smi pmon``, which additionally reports per-process SM and memory
   utilisation. The fallback is roughly ten times slower than NVML, so it runs on
   its own, slower cadence.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from .nvml import NVML, GPUDevice, GPUProcess
from .procs import ProcInfo, ProcSource
from . import drm

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GPUSnapshot:
    devices: tuple[GPUDevice, ...]
    processes: tuple[GPUProcess, ...]
    details: dict[int, ProcInfo]
    source: str  # "nvml" | "pmon" | "none"
    driver: str | None
    error: str | None
    collected_at: float


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _pmon_processes(timeout: float) -> list[GPUProcess] | None:
    """Parse ``nvidia-smi pmon -c 1``.

    The columns are, in order: gpu, pid, type, sm, mem, enc, dec, jpg, ofa,
    framebuffer MB, ccpm, command. ``-`` marks a metric the driver cannot report.
    The command may contain spaces, so only the first eleven fields are split off.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode:
        return None
    rows: list[GPUProcess] = []
    columns = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("#"):
            fields = line.lstrip("# ").split()
            if "gpu" in fields and "pid" in fields and "sm" in fields:
                columns = {name: index for index, name in enumerate(fields)}
            continue
        if not line:
            continue
        parts = line.split(None, 11)
        if len(parts) < 5:
            continue
        gpu_index = _int_or_none(parts[0])
        pid = _int_or_none(parts[1])
        if gpu_index is None or pid is None:
            continue
        fb_index = columns.get("fb") if columns else 9
        framebuffer = (
            _int_or_none(parts[fb_index])
            if fb_index is not None and fb_index < len(parts)
            else None
        )
        sm_pct = _int_or_none(parts[3])
        mem_pct = _int_or_none(parts[4])
        rows.append(
            GPUProcess(
                pid=pid,
                gpu_index=gpu_index,
                kind=parts[2] if len(parts) > 2 else "?",
                mem_bytes=framebuffer * _MIB if framebuffer is not None else None,
                sm_pct=None if sm_pct is None else sm_pct / 100.0,
                mem_pct=None if mem_pct is None else mem_pct / 100.0,
            )
        )
    return rows


class GPUSource:
    """Combines NVML device telemetry with per-process GPU usage."""

    def __init__(self, proc_source: ProcSource, *, pmon_interval: float = 2.0) -> None:
        self._nvml: NVML | None = None
        self._procs = proc_source
        self._pmon_interval = pmon_interval
        self._cached_processes: tuple[GPUProcess, ...] = ()
        self._cached_at = 0.0
        self.source = "none"
        self._process_error: str | None = None

    @property
    def nvml(self) -> NVML:
        if self._nvml is None:
            self._nvml = NVML()
        return self._nvml

    @property
    def available(self) -> bool:
        return self.nvml.available or bool(drm.devices())

    def _processes(self, force: bool = False) -> tuple[GPUProcess, ...]:
        """Per-process usage, falling back from NVML to ``nvidia-smi pmon``."""
        native = self.nvml.processes()
        if native is not None:
            self.source = "nvml"
            self._process_error = None
            self._cached_processes = tuple(native)
            self._cached_at = time.monotonic()
            return self._cached_processes

        # NVML cannot report processes on this driver.
        self.source = "pmon"
        now = time.monotonic()
        if force or now - self._cached_at >= self._pmon_interval:
            rows = _pmon_processes(timeout=3.0)
            self._cached_at = now
            if rows is not None:
                self._cached_processes = tuple(rows)
                self._process_error = None
            else:
                self._cached_processes = ()
                self._process_error = (
                    "GPU process data unavailable (nvidia-smi pmon failed)"
                )
        return self._cached_processes

    def sample(self, *, force_processes: bool = False) -> GPUSnapshot:
        native_devices = tuple(self.nvml.devices()) if self.nvml.available else ()
        extra_devices = tuple(
            drm.devices(
                start_index=max((d.index for d in native_devices), default=-1) + 1,
                skip_nvidia=self.nvml.available,
            )
        )
        if not native_devices and not extra_devices:
            return GPUSnapshot(
                devices=(),
                processes=(),
                details={},
                source="none",
                driver=None,
                error="No supported GPU telemetry detected",
                collected_at=time.time(),
            )

        devices = native_devices + extra_devices
        processes = self._processes(force=force_processes) if native_devices else ()
        source = self.source if native_devices else "sysfs"
        if native_devices and extra_devices:
            source += "+sysfs"

        # Attach the same user/command/start-time data the process panel shows, so
        # a GPU process is identifiable across panels.
        details: dict[int, ProcInfo] = {}
        for proc in processes:
            info = self._procs.describe(proc.pid)
            if info is not None:
                details[proc.pid] = info

        return GPUSnapshot(
            devices=devices,
            processes=processes,
            details=details,
            source=source,
            driver=self.nvml.driver_version,
            error=self._process_error
            if native_devices
            else "Per-process GPU usage unavailable through sysfs",
            collected_at=time.time(),
        )
