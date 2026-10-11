"""CPU statistics from ``/proc/stat``."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Offsets within the tuple produced by :func:`_totals`.
_TOTAL, _USER, _SYSTEM, _IDLE, _STEAL, _IOWAIT = range(6)

_CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"


@dataclass(frozen=True, slots=True)
class CPUStats:
    total: float
    user: float
    system: float
    iowait: float
    steal: float
    cores: tuple[tuple[str, float], ...]
    load1: float
    load5: float
    load15: float
    freq_mhz: float | None
    model: str
    online_cores: int


def _totals(line: str) -> tuple[int, ...]:
    """Reduce CPU counters to total, user, system, idle+wait, steal, and wait.

    ``idle`` includes ``iowait``; ``user`` includes ``nice``; ``system`` includes
    ``irq`` and ``softirq``. Guest time is already accounted for in ``user``.
    """
    values = [int(field) for field in line.split()[1:]]
    if len(values) < 4 or any(value < 0 for value in values):
        raise ValueError("invalid CPU counters")

    def at(index: int) -> int:
        return values[index] if index < len(values) else 0

    return (
        sum(values[:8]),  # guest/guest_nice are already in user/nice
        at(0) + at(1),
        at(2) + at(5) + at(6),
        at(3) + at(4),
        at(7),
        at(4),
    )


def _elapsed(new: tuple[int, ...], old: tuple[int, ...]) -> int:
    return new[_TOTAL] - old[_TOTAL]


def _delta_ratio(new: tuple[int, ...], old: tuple[int, ...], index: int) -> float:
    """Fraction of the elapsed interval spent in the component at ``index``."""
    elapsed = _elapsed(new, old)
    if elapsed <= 0:
        return 0.0
    return max(0.0, min(1.0, (new[index] - old[index]) / elapsed))


def _busy_ratio(new: tuple[int, ...], old: tuple[int, ...]) -> float:
    """Fraction of the elapsed interval that was not idle.

    Must exclude idle (which already folds in iowait) rather than reading the total
    directly, which would trivially divide by itself and always yield 1.0.
    """
    elapsed = _elapsed(new, old)
    if elapsed <= 0:
        return 0.0
    idle = new[_IDLE] - old[_IDLE]
    return max(0.0, min(1.0, (elapsed - idle) / elapsed))


def _read_text(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def _read_freq() -> float | None:
    raw = _read_text(_CPUFREQ).strip()
    if raw.isdigit():
        return int(raw) / 1000.0
    for line in _read_text("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            try:
                return float(line.split(":", 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def _read_model() -> str:
    for line in _read_text("/proc/cpuinfo").splitlines():
        if (
            line.split(":", 1)[0].strip() in ("model name", "Hardware", "Processor")
            and ":" in line
        ):
            return line.split(":", 1)[1].strip()
    return "CPU"


def _read_load() -> tuple[float, float, float]:
    fields = _read_text("/proc/loadavg").split()
    if len(fields) >= 3:
        try:
            return (float(fields[0]), float(fields[1]), float(fields[2]))
        except ValueError:
            pass
    return (0.0, 0.0, 0.0)


class CPUSource:
    """Samples ``/proc/stat``, deriving rates from tick deltas between samples."""

    def __init__(self) -> None:
        self._prev: dict[str, tuple[int, ...]] = {}
        self.model: str | None = None
        self.online_cores = os.cpu_count() or 1

    def sample(self) -> CPUStats:
        if self.model is None:
            self.model = _read_model()
        current: dict[str, tuple[int, ...]] = {}
        for line in _read_text("/proc/stat").splitlines():
            fields = line.split()
            if not fields:
                continue
            label = fields[0]
            if label != "cpu" and not (label.startswith("cpu") and label[3:].isdigit()):
                break  # CPU lines always come first; stop at cpu_mhz / intr
            try:
                current[label] = _totals(line)
            except ValueError:
                continue

        if "cpu" not in current:
            raise OSError("CPU counters unavailable: /proc/stat is not readable")

        previous = self._prev
        self._prev = current

        def share(index: int) -> float:
            """A component's share of the interval, on the same 0..1 scale."""
            new, old = current.get("cpu"), previous.get("cpu")
            if new is None or old is None:
                return 0.0
            return _delta_ratio(new, old, index)

        def core_rate(label: str) -> float:
            new, old = current.get(label), previous.get(label)
            if new is None or old is None:
                return 0.0
            return _busy_ratio(new, old)

        new_agg, old_agg = current.get("cpu"), previous.get("cpu")
        total = (
            _busy_ratio(new_agg, old_agg)
            if new_agg is not None and old_agg is not None
            else 0.0
        )
        user, system, iowait, steal = (
            share(_USER),
            share(_SYSTEM),
            share(_IOWAIT),
            share(_STEAL),
        )

        cores = tuple((label, core_rate(label)) for label in current if label != "cpu")
        load1, load5, load15 = _read_load()
        return CPUStats(
            total=total,
            user=user,
            system=system,
            iowait=iowait,
            steal=steal,
            cores=cores,
            load1=load1,
            load5=load5,
            load15=load15,
            freq_mhz=_read_freq(),
            model=self.model,
            online_cores=len(cores) or self.online_cores,
        )
