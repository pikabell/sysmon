"""Optional read-only DRM/sysfs telemetry, independent of vendor tools.

AMDGPU exposes busy and VRAM counters. Other drivers (including Intel) may
expose only device identity and hwmon readings; missing values remain unknown.
"""

from __future__ import annotations

import re
from pathlib import Path

from .nvml import GPUDevice

DRM_ROOT = Path("/sys/class/drm")
VENDORS = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _number(path: Path) -> int | None:
    try:
        return int(_read(path))
    except ValueError:
        return None


def devices(
    root: Path = DRM_ROOT, *, start_index: int = 0, skip_nvidia: bool = False
) -> list[GPUDevice]:
    result = []
    seen = set()
    for card in sorted(root.glob("card*"), key=lambda path: path.name):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        vendor = _read(device / "vendor")
        if not vendor or (vendor == "0x10de" and skip_nvidia):
            continue
        identity = device.resolve()
        if identity in seen:
            continue
        seen.add(identity)
        driver = (
            (device / "driver").resolve().name if (device / "driver").exists() else ""
        )
        hwmons = sorted((device / "hwmon").glob("hwmon*"))
        hwmon = hwmons[0] if hwmons else device / "hwmon-unavailable"
        temp = _number(hwmon / "temp1_input")
        power = _number(hwmon / "power1_average")
        if power is None:
            power = _number(hwmon / "power1_input")
        cap = _number(hwmon / "power1_cap")
        busy = _number(device / "gpu_busy_percent")
        mem_busy = _number(device / "mem_busy_percent")
        result.append(
            GPUDevice(
                index=start_index + len(result),
                name=f"{VENDORS.get(vendor, vendor)} {_read(device / 'device')} {driver}".strip(),
                util_gpu=busy if busy is not None and 0 <= busy <= 100 else None,
                util_mem=mem_busy
                if mem_busy is not None and 0 <= mem_busy <= 100
                else None,
                mem_used=_number(device / "mem_info_vram_used"),
                mem_total=_number(device / "mem_info_vram_total"),
                temperature=round(temp / 1000) if temp is not None else None,
                power_w=power / 1_000_000 if power is not None else None,
                power_limit_w=cap / 1_000_000 if cap is not None else None,
                fan=None,
                clock_mhz=None,
                mem_clock_mhz=None,
            )
        )
    return result
