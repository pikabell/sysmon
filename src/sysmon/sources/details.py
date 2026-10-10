"""Bounded, identity-checked process inspection. Never dereferences open files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .procs import CLOCK_TICKS, ProcInfo, ProcSource, _parse_stat


@dataclass(frozen=True, slots=True)
class ProcessDetails:
    info: ProcInfo
    status: tuple[tuple[str, str], ...]
    io: tuple[tuple[str, int], ...]
    limits: str
    files: tuple[tuple[str, str], ...]
    threads: tuple[tuple[int, str, str], ...]
    notes: tuple[str, ...]


def inspect_process(
    source: ProcSource, expected: ProcInfo, proc: Path = Path("/proc")
) -> ProcessDetails:
    def matches(info):
        if info is None:
            return False
        if expected.start_ticks is not None and info.start_ticks is not None:
            return expected.start_ticks == info.start_ticks
        if info.start_ts is None or expected.start_ts is None:
            return False
        return abs(info.start_ts - expected.start_ts) <= 0.5 / CLOCK_TICKS

    before = source.describe(expected.pid)
    if not matches(before):
        raise ProcessLookupError("process exited or PID was reused")
    base = proc / str(expected.pid)
    notes = []

    def read(name: str) -> str:
        try:
            return (base / name).read_text(errors="replace")
        except PermissionError:
            notes.append(f"{name}: permission denied")
        except OSError as exc:
            notes.append(f"{name}: {exc.strerror}")
        return ""

    status = []
    wanted = {
        "Uid",
        "Gid",
        "VmSize",
        "VmRSS",
        "VmSwap",
        "Threads",
        "FDSize",
        "Cpus_allowed_list",
        "Mems_allowed_list",
        "voluntary_ctxt_switches",
        "nonvoluntary_ctxt_switches",
    }
    for line in read("status").splitlines():
        key, _, value = line.partition(":")
        if key in wanted:
            status.append((key, value.strip()))
    io = []
    for line in read("io").splitlines():
        key, _, value = line.partition(":")
        try:
            io.append((key, int(value)))
        except ValueError:
            notes.append("io: malformed counter")
    limits = read("limits")
    files = []
    try:
        with os.scandir(base / "fd") as entries:
            for entry in entries:
                if len(files) >= 128:
                    notes.append("open files: truncated after 128 entries")
                    break
                try:
                    files.append((entry.name, os.readlink(entry.path)))
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    notes.append(f"fd {entry.name}: {exc.strerror}")
        files.sort(key=lambda item: int(item[0]))
    except OSError as exc:
        notes.append(f"open files: {exc.strerror}")
    threads = []
    try:
        with os.scandir(base / "task") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                if len(threads) >= 256:
                    notes.append("threads: truncated after 256 entries")
                    break
                try:
                    raw = _parse_stat(
                        int(entry.name), (Path(entry.path) / "stat").read_bytes()
                    )
                    if raw:
                        threads.append((raw.pid, raw.name, raw.state))
                except OSError:
                    continue
    except OSError as exc:
        notes.append(f"threads: {exc.strerror}")
    after = source.describe(expected.pid)
    if not matches(after):
        raise ProcessLookupError(
            "process exited or PID was reused while reading details"
        )
    return ProcessDetails(
        before,
        tuple(status),
        tuple(io),
        limits,
        tuple(files),
        tuple(sorted(threads)),
        tuple(notes),
    )
