"""Read-only PSI: percentages of stalled time, not resource utilisation.

Format/semantics: https://docs.kernel.org/accounting/psi.html
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PressureLine:
    avg10: float
    avg60: float
    avg300: float
    total_us: int


@dataclass(frozen=True, slots=True)
class Pressure:
    resource: str
    some: PressureLine | None = None
    full: PressureLine | None = None
    error: str | None = None


def read_pressure(path: Path, resource: str, *, system: bool = False) -> Pressure:
    try:
        lines = {}
        for row in path.read_text().splitlines():
            kind, *parts = row.split()
            fields = dict(part.split("=", 1) for part in parts)
            averages = [float(fields[key]) for key in ("avg10", "avg60", "avg300")]
            total = int(fields["total"])
            if (
                any(
                    not math.isfinite(value) or not 0 <= value <= 100
                    for value in averages
                )
                or total < 0
            ):
                raise ValueError("invalid PSI value")
            lines[kind] = PressureLine(*averages, total)
        if "some" not in lines:
            raise ValueError("missing some pressure")
        # CPU full is undefined system-wide; the kernel's compatibility zero
        # must not appear to be a measured absence of stalls.
        full = None if system and resource == "cpu" else lines.get("full")
        return Pressure(resource, lines["some"], full)
    except PermissionError:
        return Pressure(resource, error="permission denied")
    except FileNotFoundError:
        return Pressure(resource, error="not exposed by this kernel/environment")
    except (OSError, ValueError, KeyError) as exc:
        return Pressure(resource, error=f"unavailable: {exc}")


def pressure_snapshot(
    root: Path = Path("/proc/pressure"), *, system: bool = True
) -> tuple[Pressure, ...]:
    return tuple(
        read_pressure(root / resource, resource, system=system)
        for resource in ("cpu", "memory", "io")
    )
