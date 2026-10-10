"""Block-device deltas and local mount capacity, without opening devices.

Counters: https://docs.kernel.org/admin-guide/iostats.html
Diskstats sectors are always 512 bytes, regardless of physical sector size.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

LOCAL_FILESYSTEMS = {
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "zfs",
    "f2fs",
    "jfs",
    "reiserfs",
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
    "tmpfs",
    "overlay",
    "squashfs",
}


def unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


@dataclass(frozen=True, slots=True)
class Mount:
    root: str
    path: str
    fs: str
    device: str
    options: str


def read_mounts(path: Path = Path("/proc/self/mountinfo")) -> tuple[Mount, ...]:
    result = []
    for line in path.read_text().splitlines():
        try:
            left, right = line.split(" - ", 1)
            fields, extra = left.split(), right.split()
            result.append(
                Mount(
                    unescape_mount(fields[3]),
                    unescape_mount(fields[4]),
                    extra[0],
                    unescape_mount(extra[1]),
                    extra[2],
                )
            )
        except (ValueError, IndexError):
            continue
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Disk:
    name: str
    read_rate: float | None
    write_rate: float | None
    reads_per_second: float | None
    writes_per_second: float | None
    busy: float | None
    in_flight: int
    partition: bool


@dataclass(frozen=True, slots=True)
class Filesystem:
    path: str
    device: str
    fs: str
    total: int | None = None
    used: int | None = None
    available: int | None = None
    inode_total: int | None = None
    inode_used: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    devices: tuple[Disk, ...]
    filesystems: tuple[Filesystem, ...]
    error: str | None = None


class DiskSource:
    def __init__(self, proc: Path = Path("/proc"), sys: Path = Path("/sys")):
        self.proc, self.sys = proc, sys
        self._previous: dict[str, tuple[int, ...]] = {}
        self._time: float | None = None
        self._mount_time: float | None = None
        self._filesystems: tuple[Filesystem, ...] = ()

    def sample(self) -> DiskSnapshot:
        now = time.monotonic()
        elapsed = now - self._time if self._time is not None else 0
        current, devices = {}, []
        error = None
        try:
            for line in (self.proc / "diskstats").read_text().splitlines():
                fields = line.split()
                if len(fields) < 14:
                    continue
                name = fields[2]
                try:
                    values = tuple(int(value) for value in fields[3:14])
                except ValueError:
                    continue
                if any(value < 0 for value in values):
                    continue
                current[name] = values
                old = self._previous.get(name)
                valid = (
                    elapsed > 0
                    and old is not None
                    and all(values[i] >= old[i] for i in (0, 2, 4, 6, 9))
                )
                deltas = {
                    index: (values[index] - old[index]) / elapsed if valid else None
                    for index in (0, 2, 4, 6, 9)
                }
                read, write, busy = deltas[2], deltas[6], deltas[9]
                devices.append(
                    Disk(
                        name,
                        None if read is None else read * 512,
                        None if write is None else write * 512,
                        deltas[0],
                        deltas[4],
                        None if busy is None else min(1.0, busy / 1000),
                        values[8],
                        (self.sys / "class/block" / name / "partition").is_file(),
                    )
                )
        except OSError as exc:
            error = f"disk counters unavailable: {exc}"
        self._previous, self._time = current, now
        if self._mount_time is None or now - self._mount_time >= 10:
            self._filesystems = self._capacity()
            self._mount_time = now
        return DiskSnapshot(tuple(devices), self._filesystems, error)

    def _capacity(self) -> tuple[Filesystem, ...]:
        result = []
        try:
            mounts = read_mounts(self.proc / "self/mountinfo")
        except OSError as exc:
            return (Filesystem("-", "-", "-", error=f"mounts unavailable: {exc}"),)
        seen = set()
        for mount in mounts:
            if mount.path in seen or mount.fs in {
                "proc",
                "sysfs",
                "devpts",
                "cgroup",
                "cgroup2",
                "securityfs",
                "debugfs",
                "tracefs",
                "pstore",
                "mqueue",
                "hugetlbfs",
                "configfs",
                "autofs",
                "binfmt_misc",
                "efivarfs",
                "nsfs",
                "fusectl",
                "rpc_pipefs",
            }:
                continue
            seen.add(mount.path)
            if mount.fs not in LOCAL_FILESYSTEMS:
                # A blocked network/FUSE mount can hang statvfs indefinitely.
                result.append(
                    Filesystem(
                        mount.path,
                        mount.device,
                        mount.fs,
                        error="capacity skipped for remote/unsupported filesystem",
                    )
                )
                continue
            try:
                stat = os.statvfs(mount.path)
                block = stat.f_frsize or stat.f_bsize
                result.append(
                    Filesystem(
                        mount.path,
                        mount.device,
                        mount.fs,
                        stat.f_blocks * block,
                        (stat.f_blocks - stat.f_bfree) * block,
                        max(0, stat.f_bavail) * block,
                        stat.f_files or None,
                        max(0, stat.f_files - stat.f_ffree) if stat.f_files else None,
                    )
                )
            except OSError as exc:
                result.append(
                    Filesystem(mount.path, mount.device, mount.fs, error=str(exc))
                )
        return tuple(result)
