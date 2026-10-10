"""Memory statistics from ``/proc/meminfo``."""

from __future__ import annotations

from dataclasses import dataclass

_KB = 1024


@dataclass(frozen=True, slots=True)
class MemStats:
    total: int
    available: int
    free: int
    cached: int
    buffers: int
    slab: int
    used: int
    swap_total: int
    swap_used: int
    dirty: int
    available_estimated: bool = False


class MemSource:
    """Samples ``/proc/meminfo``. Cheap enough to call every tick."""

    def sample(self) -> MemStats:
        values: dict[str, int] = {}
        try:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts and parts[0].lstrip("-").isdigit():
                        values[key] = int(parts[0]) * _KB
        except OSError as exc:
            raise OSError(
                "Memory counters unavailable: /proc/meminfo is not readable"
            ) from exc

        def kb(name: str) -> int:
            return max(0, values.get(name, 0))

        total = kb("MemTotal")
        if total <= 0:
            raise OSError("Memory counters unavailable: MemTotal is missing")
        # Shmem is not reclaimable page cache. Old kernels lack MemAvailable;
        # this fallback is an estimate, rather than mistaking cache for pressure.
        cached = max(0, kb("Cached") + kb("SReclaimable") - kb("Shmem"))
        available = min(
            total,
            max(0, values.get("MemAvailable", kb("MemFree") + kb("Buffers") + cached)),
        )
        buffers = kb("Buffers")
        slab = kb("Slab")
        free = kb("MemFree")
        dirty = kb("Dirty")
        swap_total = kb("SwapTotal")

        # Derive "used" the way free(1) does: total minus everything still
        # reclaimable without swapping.
        used = max(0, total - available)

        return MemStats(
            total=total,
            available=available,
            free=free,
            cached=cached,
            buffers=buffers,
            slab=slab,
            used=used,
            swap_total=swap_total,
            swap_used=max(0, swap_total - kb("SwapFree")),
            dirty=dirty,
            available_estimated="MemAvailable" not in values,
        )
