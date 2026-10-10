"""Process table, including exact wall-clock start times.

The hot path reads a single ``/proc/<pid>/stat`` file per process: that one file
yields the command name, state, CPU jiffies, thread count, RSS *and* the start
time, which keeps a full sweep of ~650 processes well under 100 ms.

Start time is recovered exactly, not relative to when this program launched:

    start_wall_clock = btime + starttime / SC_CLK_TCK

where ``btime`` is the boot timestamp from ``/proc/stat`` and ``starttime`` is field
22 of ``/proc/<pid>/stat`` (ticks since boot). A process therefore reports the same
age regardless of when the monitor was started or restarted.
"""

from __future__ import annotations

import os
import pwd
import time
from dataclasses import dataclass, replace

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
PROC = "/proc"

# Field offsets within /proc/<pid>/stat, counted after the ")" that closes comm.
_F_STATE = 0  # field 3
_F_PPID = 1  # field 4
_F_UTIME = 11  # field 14
_F_STIME = 12  # field 15
_F_THREADS = 17  # field 20
_F_STARTTIME = 19  # field 22
_F_RSS = 21  # field 24, in pages

STATES = {
    "R": "Running",
    "S": "Sleeping",
    "D": "Disk",
    "Z": "Zombie",
    "T": "Stopped",
    "t": "Trace",
    "X": "Dead",
    "x": "Dead",
    "K": "Wakekill",
    "W": "Waking",
    "I": "Idle",
    "P": "Paged",
}


@dataclass(frozen=True, slots=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str
    state: str
    cpu_pct: float
    rss_bytes: int
    threads: int
    cpu_seconds: float
    start_ts: float | None
    age: float | None
    username: str
    cmdline: str
    start_ticks: int | None = None

    @property
    def state_label(self) -> str:
        return STATES.get(self.state, self.state)


@dataclass(frozen=True, slots=True)
class _Raw:
    """The subset of a process's state that lives in ``/proc/<pid>/stat``."""

    pid: int
    name: str
    state: str
    ppid: int
    ticks: int
    threads: int
    starttime: int
    rss_bytes: int


def _boot_time() -> float | None:
    try:
        with open(f"{PROC}/stat") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:  # Coarse fallback: uptime is still better than nothing.
        with open(f"{PROC}/uptime") as handle:
            return time.time() - float(handle.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None


def _read_cmdline(pid: int, name: str) -> str:
    try:
        with open(f"{PROC}/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return f"[{name}]"
    parts = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    # Kernel threads expose an empty cmdline; bracket the name like ps does.
    return " ".join(parts) if parts else f"[{name}]"


def _uptime() -> float | None:
    try:
        with open(f"{PROC}/uptime") as handle:
            value = float(handle.read().split()[0])
            return value if 0 <= value < float("inf") else None
    except (OSError, ValueError, IndexError):
        return None


def _parse_stat(pid: int, raw: bytes) -> _Raw | None:
    """Parse a ``/proc/<pid>/stat`` blob, tolerating odd comm values.

    ``comm`` is parenthesised and may itself contain spaces or parentheses, so the
    split is anchored on the *last* ``)``.
    """
    try:
        close = raw.rindex(b")")
        name = raw[raw.index(b"(") + 1 : close].decode("utf-8", "replace")
        fields = raw[close + 2 :].split()
        return _Raw(
            pid=pid,
            name=name,
            state=fields[_F_STATE].decode("ascii", "replace"),
            ppid=int(fields[_F_PPID]),
            ticks=int(fields[_F_UTIME]) + int(fields[_F_STIME]),
            threads=int(fields[_F_THREADS]),
            starttime=int(fields[_F_STARTTIME]),
            rss_bytes=int(fields[_F_RSS]) * PAGE_SIZE,
        )
    except (ValueError, IndexError):
        return None


class ProcSource:
    """Maintains the process table and per-process CPU deltas across samples."""

    def __init__(self) -> None:
        self.boot_time: float | None = None
        self._ticks: dict[int, tuple[int, int]] = {}
        self._prev_sample: float | None = None
        # cmdline is cached per pid, keyed on starttime so recycled pids refresh.
        self._cmdlines: dict[int, tuple[int, str, str, float]] = {}
        self._users: dict[int, str] = {}

    def _username(self, uid: int) -> str:
        name = self._users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            self._users[uid] = name
        return name

    def _decorate(
        self, entry: _Raw, now_wall: float, uptime: float | None = None
    ) -> ProcInfo:
        """Attach the fields that live outside ``/proc/<pid>/stat``."""
        cached = self._cmdlines.get(entry.pid)
        if (
            cached is not None
            and cached[0] == entry.starttime
            and cached[1] == entry.name
            and abs(now_wall - cached[3]) < 5
        ):
            cmdline = cached[2]
        else:
            cmdline = _read_cmdline(entry.pid, entry.name)
            self._cmdlines[entry.pid] = (entry.starttime, entry.name, cmdline, now_wall)

        try:
            uid = os.stat(f"{PROC}/{entry.pid}").st_uid
        except OSError:
            uid = -1

        if self.boot_time is None:
            self.boot_time = _boot_time()
        started = (
            self.boot_time + entry.starttime / CLOCK_TICKS
            if self.boot_time is not None
            else None
        )
        age = (
            (uptime - entry.starttime / CLOCK_TICKS)
            if uptime is not None
            else (now_wall - started if started is not None else None)
        )
        return ProcInfo(
            pid=entry.pid,
            ppid=entry.ppid,
            name=entry.name,
            state=entry.state,
            cpu_pct=0.0,
            rss_bytes=entry.rss_bytes,
            threads=entry.threads,
            cpu_seconds=entry.ticks / CLOCK_TICKS,
            start_ts=started,
            age=None if age is None else max(0.0, age),
            username=self._username(uid),
            cmdline=cmdline,
            start_ticks=entry.starttime,
        )

    def describe(self, pid: int) -> ProcInfo | None:
        """Describe one pid without disturbing the sampling state.

        Used by other panels (e.g. the GPU process table) so a process appears with
        the same user, command and start time wherever it is shown.
        """
        try:
            with open(f"{PROC}/{pid}/stat", "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        entry = _parse_stat(pid, raw)
        return None if entry is None else self._decorate(entry, time.time(), _uptime())

    def sample(self) -> dict[int, ProcInfo]:
        """Full process sweep, with CPU percentages from tick deltas."""
        now_wall = time.time()
        now_mono = time.monotonic()
        uptime = _uptime()
        elapsed = (
            None
            if self._prev_sample is None
            else max(1e-6, now_mono - self._prev_sample)
        )
        previous_ticks = self._ticks
        ticks_now: dict[int, tuple[int, int]] = {}
        table: dict[int, ProcInfo] = {}

        for entry in os.scandir(PROC):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                with open(f"{entry.path}/stat", "rb") as handle:
                    raw = handle.read()
            except (OSError, ValueError):
                continue
            parsed = _parse_stat(pid, raw)
            if parsed is None:
                continue

            ticks_now[pid] = (parsed.starttime, parsed.ticks)

            cpu_pct = 0.0
            prior = previous_ticks.get(pid)
            if (
                elapsed is not None
                and prior is not None
                and prior[0] == parsed.starttime
            ):
                delta = parsed.ticks - prior[1]
                if delta > 0:
                    # May exceed 100% for multithreaded processes.
                    cpu_pct = delta / CLOCK_TICKS / elapsed * 100.0

            info = self._decorate(parsed, now_wall, uptime)
            if cpu_pct:
                info = replace(info, cpu_pct=cpu_pct)
            table[pid] = info

        self._ticks = ticks_now
        self._prev_sample = now_mono
        # Drop cache entries for processes that have exited.
        self._cmdlines = {
            pid: value for pid, value in self._cmdlines.items() if pid in table
        }
        return table
