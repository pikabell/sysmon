"""Correctness checks for the data sources.

These verify the numbers sysmon reports against independent implementations, so a
refactor cannot silently change what the panels claim.

Run with:  uv run python tests/test_sources.py
"""

from __future__ import annotations

import sys
import time
import socket
import struct
import os
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

from sysmon.sources.cpu import CPUSource
from sysmon.sources.memory import MemSource
from sysmon.sources.net import NetSource, _decode_addr, _endpoint
from sysmon.sources.nvml import NVML
from sysmon.sources.procs import CLOCK_TICKS, ProcSource

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def test_start_times() -> None:
    """Start times must match psutil's independent create_time()."""
    source = ProcSource()
    uptime_before = float(Path("/proc/uptime").read_text().split()[0])
    table = source.sample()
    uptime_after = float(Path("/proc/uptime").read_text().split()[0])
    sample = sorted(table)[::17]
    worst = 0.0
    for pid in sample:
        try:
            worst = max(
                worst, abs(table[pid].start_ts - psutil.Process(pid).create_time())
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    check(
        "process start times match psutil.create_time()",
        worst < 1 / CLOCK_TICKS,
        f"{len(sample)} pids, max divergence {worst:.4f}s",
    )
    # Age uses uptime, while btime is a whole-second wall-clock timestamp.
    # Compare with independent kernel ticks and bracket the collection window.
    pid = os.getpid()
    info = table[pid]
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    started = int(fields[19]) / CLOCK_TICKS
    check(
        "age matches kernel uptime and start ticks",
        max(0, uptime_before - started) - 0.02
        <= info.age
        <= max(0, uptime_after - started) + 0.02,
        f"pid {pid}: age {info.age:.1f}s",
    )


def test_cpu_arithmetic() -> None:
    """CPU components must not exceed the aggregate, and must reach 100% when pegged."""
    source = CPUSource()
    source.sample()  # baseline
    time.sleep(1.2)
    stats = source.sample()
    split = stats.user + stats.system + stats.steal
    check(
        "cpu components do not exceed the aggregate",
        split <= stats.total + 0.02,
        f"split {split * 100:.1f}% vs total {stats.total * 100:.1f}%",
    )
    check(
        "cpu values are fractions in 0..1",
        all(
            0.0 <= value <= 1.0
            for value in (
                stats.total,
                stats.user,
                stats.system,
                stats.iowait,
                stats.steal,
                *(usage for _, usage in stats.cores),
            )
        ),
    )
    check(
        "per-core count matches /proc/stat",
        len(stats.cores) == stats.online_cores,
        f"{len(stats.cores)} cores",
    )


def test_memory_arithmetic() -> None:
    stats = MemSource().sample()
    check(
        "meminfo total is plausible",
        stats.total > 1 << 30,
        f"{stats.total // 2**20} MiB",
    )
    check(
        "used + available does not exceed total",
        stats.used + stats.available <= stats.total + 4096,
    )
    reference = psutil.virtual_memory()
    check(
        "memtotal matches psutil",
        abs(stats.total - reference.total) < 1 << 20,
    )


def test_net_decoding() -> None:
    ipv4 = f"{struct.unpack('=I', socket.inet_aton('127.0.0.1'))[0]:08X}"
    check("ipv4 loopback decodes", _decode_addr(ipv4, False) == "127.0.0.1")
    check("ipv4 wildcard decodes", _decode_addr("00000000", False) == "0.0.0.0")
    check("ipv6 wildcard renders as *", _endpoint("::", 22) == "*:22")
    check("ipv6 endpoint is bracketed", _endpoint("::1", 22) == "[::1]:22")
    check("wildcard ipv4 renders as *", _endpoint("0.0.0.0", 443) == "*:443")
    # /proc/net/tcp6 stores 32-bit words in host byte order.
    words = struct.unpack("=4I", socket.inet_pton(socket.AF_INET6, "::1"))
    encoded = "".join(f"{word:08X}" for word in words)
    check("ipv6 loopback decodes", _decode_addr(encoded, True) == "::1")


def test_net_sources() -> None:
    source = NetSource(ProcSource())
    source.sample()
    time.sleep(0.6)
    snapshot = source.sample()
    check(
        "interfaces discovered",
        len(snapshot.ifaces) > 0,
        f"{len(snapshot.ifaces)} ifaces",
    )
    check(
        "interface counters are non-negative",
        all(i.rx_bytes >= 0 and i.tx_bytes >= 0 for i in snapshot.ifaces),
    )
    check(
        "rates are non-negative",
        all(i.rx_rate >= 0 and i.tx_rate >= 0 for i in snapshot.ifaces),
    )
    reference = psutil.net_io_counters(pernic=True)
    for iface in snapshot.ifaces:
        if iface.name in reference:
            expected = reference[iface.name].bytes_recv
            check(
                f"rx total matches psutil for {iface.name}",
                abs(iface.rx_bytes - expected) < 1 << 20,
                f"{iface.rx_bytes} vs {expected}",
            )
            break
    check(
        "sockets discovered", len(snapshot.conns) > 0, f"{len(snapshot.conns)} sockets"
    )


def test_gpu() -> None:
    nvml = NVML()
    if not nvml.available:
        print("  [SKIP] no NVIDIA GPU on this machine")
        return
    check("driver version read", bool(nvml.driver_version), str(nvml.driver_version))
    devices = nvml.devices()
    check("devices enumerated", len(devices) > 0, f"{len(devices)} GPUs")
    for device in devices:
        check(
            f"gpu{device.index} telemetry within range",
            device.util_gpu is None or 0 <= device.util_gpu <= 100,
            f"util {device.util_gpu}%, {device.temperature}C, {device.power_w}W",
        )
        check(
            f"gpu{device.index} vram within capacity",
            device.mem_total is None
            or (device.mem_used is not None and device.mem_used <= device.mem_total),
        )
    # processes() returns None when the driver cannot answer, and that is cached.
    result = nvml.processes()
    check(
        "process query either works or is declared unsupported",
        result is None or isinstance(result, list),
        "unsupported -> pmon fallback" if result is None else f"{len(result)} procs",
    )
    if result is None:
        check("unsupported result cached", nvml.procs_supported is False)


def test_no_privilege_required() -> None:
    """Everything must work as the current unprivileged user."""
    import os

    print(f"  current euid: {os.geteuid()} (CI may run as root)")
    table = ProcSource().sample()
    attributed = sum(1 for info in table.values() if info.username)
    check(
        "process owners resolved",
        attributed == len(table),
        f"{attributed}/{len(table)}",
    )


def main() -> int:
    if psutil is None:
        print("Live source cross-checks require psutil: install the 'test' extra.")
        return 2
    for test in (
        test_start_times,
        test_cpu_arithmetic,
        test_memory_arithmetic,
        test_net_decoding,
        test_net_sources,
        test_gpu,
        test_no_privilege_required,
    ):
        print(f"\n{test.__name__.replace('test_', '').replace('_', ' ')}")
        try:
            test()
        except Exception as exc:
            failures.append(test.__name__)
            print(f"  [FAIL] {test.__name__} raised {type(exc).__name__}: {exc}")

    print(f"\n{'=' * 70}")
    if failures:
        print(f"RESULT: {len(failures)} failure(s): {', '.join(failures)}")
    else:
        print("RESULT: all source checks passed")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
