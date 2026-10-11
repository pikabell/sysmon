"""System uptime and the monitor's visible cgroup limits/usage.

Reads v2 and legacy v1; ancestor limits constrain descendants. Nothing here
creates cgroups, changes quotas, or requires a Docker daemon.
Reference: https://docs.kernel.org/admin-guide/cgroup-v2.html
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .disk import Mount, read_mounts
from .pressure import Pressure, pressure_snapshot, read_pressure


def cpuset_count(value: str) -> int:
    spans = []
    for item in value.strip().split(","):
        if not item:
            continue
        ends = item.split("-")
        start, end = int(ends[0]), int(ends[-1])
        if len(ends) > 2 or start < 0 or end < start:
            raise ValueError("invalid CPU set")
        spans.append((start, end))
    total, previous = 0, -1
    for start, end in sorted(spans):
        total += max(0, end - max(start, previous + 1) + 1)
        previous = max(previous, end)
    return total


def locate(mount: Mount, membership: str) -> Path | None:
    path, root = PurePosixPath(membership), PurePosixPath(mount.root)
    if ".." in path.parts or not path.is_absolute():
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        # Namespaced procfs can report '/' relative to a cgroup mount root.
        if membership != "/":
            return None
        relative = PurePosixPath(".")
    return Path(mount.path) / str(relative)


def ancestors(path: Path, boundary: Path):
    if not path.is_relative_to(boundary):
        return
    while True:
        yield path
        if path == boundary:
            return
        path = path.parent


@dataclass(frozen=True, slots=True)
class CgroupStats:
    version: str | None = None
    path: str | None = None
    cpu_capacity: float | None = None
    quota_cpus: float | None = None
    cpu_pct: float | None = None
    memory_used: int | None = None
    memory_limit: int | None = None
    memory_capacity: int | None = None
    pressure: tuple[Pressure, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    uptime: float | None
    pressure: tuple[Pressure, ...]
    cgroup: CgroupStats
    uptime_error: str | None = None


class ResourceSource:
    def __init__(self, proc: Path = Path("/proc")):
        self.proc = proc
        self._cpu_previous: tuple[str, int, float] | None = None

    def sample(self) -> ResourceSnapshot:
        uptime, error = None, None
        try:
            uptime = float((self.proc / "uptime").read_text().split()[0])
            if not 0 <= uptime < float("inf"):
                raise ValueError("invalid uptime")
        except (OSError, ValueError, IndexError) as exc:
            uptime = None
            error = str(exc)
        return ResourceSnapshot(
            uptime, pressure_snapshot(self.proc / "pressure"), self._cgroup(), error
        )

    def _cgroup(self) -> CgroupStats:
        errors: list[str] = []

        def read(path: Path, required: bool = False) -> str | None:
            try:
                return path.read_text().strip()
            except FileNotFoundError:
                if required:
                    errors.append(f"{path.name}: not exposed")
            except OSError as exc:
                errors.append(f"{path.name}: {exc.strerror}")
            return None

        def number(path: Path, required=False) -> int | None:
            value = read(path, required)
            if value is None or value == "max":
                return None
            try:
                result = int(value)
                if result < 0:
                    if path.name == "cpu.cfs_quota_us" and result == -1:
                        return None
                    errors.append(f"{path.name}: invalid negative counter")
                    return None
                return result
            except ValueError:
                errors.append(f"{path.name}: invalid number")
                return None

        try:
            members = {}
            for line in (self.proc / "self/cgroup").read_text().splitlines():
                _, controllers, membership = line.split(":", 2)
                for controller in controllers.split(","):
                    members[controller] = membership
            mounts = read_mounts(self.proc / "self/mountinfo")
        except (OSError, ValueError) as exc:
            self._cpu_previous = None
            return CgroupStats(errors=(f"cgroups unavailable: {exc}",))
        try:
            host_cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            host_cpus = os.cpu_count() or 1
        host_memory = None
        try:
            for row in (self.proc / "meminfo").read_text().splitlines():
                if row.startswith("MemTotal:"):
                    host_memory = int(row.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        v2_mounts = sorted(
            (mount for mount in mounts if mount.fs == "cgroup2" and "" in members),
            key=lambda mount: len(PurePosixPath(mount.root).parts),
        )
        v2 = next(
            (mount for mount in v2_mounts if locate(mount, members[""]) is not None),
            v2_mounts[0] if v2_mounts else None,
        )
        quota, memory_limits, used, usage = [], [], None, None
        version, path, pressure = None, None, ()
        cpu_path = None
        if v2:
            location = locate(v2, members[""])
            if location is None:
                self._cpu_previous = None
                return CgroupStats(
                    version="v2",
                    path=members[""],
                    errors=("cgroup path outside visible mount",),
                )
            version, path, cpu_path = "v2", members[""], str(location)
            for parent in ancestors(location, Path(v2.path)):
                value = read(parent / "cpu.max")
                if value is not None:
                    try:
                        limit, period = value.split()
                        if int(period) <= 0:
                            raise ValueError()
                        if limit != "max":
                            ratio = int(limit) / int(period)
                            if ratio <= 0:
                                raise ValueError()
                            quota.append(ratio)
                    except (ValueError, ZeroDivisionError):
                        errors.append("cpu.max: invalid quota")
                value = number(parent / "memory.max")
                if value is not None:
                    memory_limits.append(value)
            value = read(location / "cpuset.cpus.effective")
            if value:
                try:
                    host_cpus = min(host_cpus, cpuset_count(value))
                except ValueError:
                    errors.append("cpuset: invalid CPU set")
            value = read(location / "cpu.stat")
            if value:
                try:
                    counters = dict(row.split() for row in value.splitlines())
                    usage = int(counters["usage_usec"]) * 1000
                    if usage < 0:
                        raise ValueError()
                except (ValueError, KeyError):
                    usage = None
                    errors.append("cpu.stat: usage unavailable")
            used = number(location / "memory.current")
            pressure = tuple(
                read_pressure(location / f"{resource}.pressure", resource)
                for resource in ("cpu", "memory", "io")
            )
        if not v2 or any(
            controller in members
            for controller in ("cpu", "cpuacct", "memory", "cpuset")
        ):
            locations = {}
            for controller in ("cpu", "cpuacct", "memory", "cpuset"):
                mount = next(
                    (
                        m
                        for m in sorted(
                            mounts,
                            key=lambda mount: len(PurePosixPath(mount.root).parts),
                        )
                        if m.fs == "cgroup"
                        and controller in m.options.split(",")
                        and controller in members
                        and locate(m, members[controller]) is not None
                    ),
                    None,
                )
                if mount:
                    location = locate(mount, members[controller])
                    if location is not None:
                        locations[controller] = (location, Path(mount.path))
            if not locations and not v2:
                self._cpu_previous = None
                return CgroupStats(errors=("no readable cgroup hierarchy exposed",))
            version = "hybrid" if v2 else "v1"
            path = (
                "v2:"
                + members[""]
                + "; "
                + "; ".join(
                    f"{controller}:{members[controller]}"
                    for controller in ("cpu", "cpuacct", "memory", "cpuset")
                    if controller in members
                )
                if v2
                else members.get("cpu", members.get("memory"))
            )
            # Controllers bound to v1 constrain this monitor even when a v2
            # systemd hierarchy is also mounted. Missing legacy mounts must not
            # look like unlimited capacity, nor mix usage from another hierarchy.
            for controller, prefix in (
                ("cpu", "cpu.cfs_quota_us"),
                ("memory", "memory.limit_in_bytes"),
                ("cpuset", "cpuset"),
            ):
                if controller in members and controller not in locations:
                    errors.append(f"{prefix}: hierarchy outside visible mounts")
            if "cpu" in members or "cpuacct" in members:
                usage, cpu_path = None, None
            if "memory" in members:
                used = None
            if "cpu" in locations:
                location, boundary = locations["cpu"]
                for parent in ancestors(location, boundary):
                    limit = number(parent / "cpu.cfs_quota_us")
                    if limit is not None:
                        period = number(parent / "cpu.cfs_period_us", required=True)
                        if limit <= 0:
                            errors.append("cpu.cfs_quota_us: invalid quota")
                        elif period is None or period <= 0:
                            errors.append("cpu.cfs_period_us: invalid period")
                        else:
                            quota.append(limit / period)
            if "cpuacct" in locations:
                location, _ = locations["cpuacct"]
                cpu_path = str(location)
                usage = number(location / "cpuacct.usage")
            if "memory" in locations:
                location, boundary = locations["memory"]
                used = number(location / "memory.usage_in_bytes")
                for parent in ancestors(location, boundary):
                    if (
                        parent != location
                        and read(parent / "memory.use_hierarchy") == "0"
                    ):
                        continue
                    value = number(parent / "memory.limit_in_bytes")
                    if value is not None and value < 2**60:
                        memory_limits.append(value)
            if "cpuset" in locations:
                location, boundary = locations["cpuset"]
                for parent in ancestors(location, boundary):
                    value = read(parent / "cpuset.cpus")
                    if value:
                        try:
                            host_cpus = min(host_cpus, cpuset_count(value))
                        except ValueError:
                            errors.append("cpuset: invalid CPU set")
                        break
        capacity = min(float(host_cpus), min(quota)) if quota else float(host_cpus)
        if capacity <= 0 or any(
            message.startswith(("cpu.max", "cpu.cfs", "cpuset")) for message in errors
        ):
            capacity = None
        memory_limit = min(memory_limits) if memory_limits else None
        memory_capacity = (
            (
                min(memory_limit, host_memory)
                if host_memory is not None
                else memory_limit
            )
            if memory_limit is not None
            else host_memory
        )
        if any(
            message.startswith(("memory.max", "memory.limit")) for message in errors
        ):
            memory_capacity = None
        now, cpu_pct = time.monotonic(), None
        previous = self._cpu_previous
        if usage is not None and cpu_path:
            if (
                previous
                and previous[0] == cpu_path
                and usage >= previous[1]
                and now > previous[2]
                and capacity
            ):
                cpu_pct = (
                    (usage - previous[1]) / 1e9 / (now - previous[2]) / capacity * 100
                )
            self._cpu_previous = (cpu_path, usage, now)
        else:
            self._cpu_previous = None
        return CgroupStats(
            version,
            path,
            capacity,
            min(quota) if quota else None,
            cpu_pct,
            used,
            memory_limit,
            memory_capacity,
            pressure,
            tuple(errors),
        )
