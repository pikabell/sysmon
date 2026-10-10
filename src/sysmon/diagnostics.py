"""A read-only capability report for installations on unfamiliar Linux hosts."""

from __future__ import annotations

import platform
import sys
from pathlib import Path


def check() -> tuple[str, int]:
    lines = [
        f"sysmon capabilities · {platform.system()} {platform.machine()}",
        f"Python {platform.python_version()} · kernel {platform.release()}",
    ]
    required_ok = sys.platform.startswith("linux")
    for path in ("/proc/stat", "/proc/meminfo", "/proc/net/dev"):
        try:
            with open(path) as handle:
                readable = bool(handle.read(256))
        except OSError:
            readable = False
        lines.append(f"{'OK' if readable else 'UNAVAILABLE'}  {path}")
        required_ok &= readable
    lines.append("GPU metrics: optional; NVIDIA NVML or readable DRM/sysfs attributes")
    lines.append(
        f"DRM sysfs: {'present' if Path('/sys/class/drm').is_dir() else 'absent'}"
    )
    for path in (
        "/proc/uptime",
        "/proc/diskstats",
        "/proc/pressure/cpu",
        "/proc/pressure/memory",
        "/proc/pressure/io",
        "/proc/self/cgroup",
        "/proc/self/mountinfo",
    ):
        try:
            with open(path) as handle:
                readable = bool(handle.read(256))
        except OSError:
            readable = False
        lines.append(f"{'OK' if readable else 'OPTIONAL UNAVAILABLE'}  {path}")
    lines.append(
        "Process and socket visibility follow kernel permissions and namespaces."
    )
    return "\n".join(lines), 0 if required_ok else 2
