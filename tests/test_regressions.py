"""Deterministic metric, portability and read-only regression tests.

Run with: python -m unittest discover -s tests -p test_regressions.py
"""

from __future__ import annotations

import ctypes
import io
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from rich.console import Console

from sysmon.core.render import bar_cells
from sysmon.panels.overview import OverviewPanel
from sysmon.sources.cpu import CPUSource
from sysmon.sources.memory import MemSource
from sysmon.sources.net import NetSource, _decode_addr
from sysmon.sources.nvml import (
    NVML,
    NVML_ERROR_NOT_SUPPORTED,
    NVML_ERROR_INSUFFICIENT_SIZE,
)
from sysmon.sources.procs import ProcSource
from sysmon.sources.gpu import GPUSource, _pmon_processes
from sysmon.sources import drm


class Metrics(unittest.TestCase):
    def test_source_construction_does_not_block_on_io(self):
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected read")),
            patch(
                "sysmon.sources.gpu.NVML",
                side_effect=AssertionError("unexpected driver load"),
            ),
        ):
            cpu, proc = CPUSource(), ProcSource()
            GPUSource(proc)
        self.assertIsNone(cpu.model)
        self.assertIsNone(proc.boot_time)

    def cpu_samples(self, old, new):
        source = CPUSource()

        def read(path):
            return old if path == "/proc/stat" else ""

        with patch("sysmon.sources.cpu._read_text", side_effect=read):
            source.sample()

        def read_new(path):
            return new if path == "/proc/stat" else ""

        with patch("sysmon.sources.cpu._read_text", side_effect=read_new):
            return source.sample()

    def test_cpu_fields_and_guest_not_double_counted(self):
        stats = self.cpu_samples(
            "cpu 100 0 100 800 0 0 0 0 0 0\ncpu7 100 0 100 800 0 0 0 0 0 0",
            "cpu 140 10 120 850 20 5 5 10 30 5\ncpu7 140 10 120 850 20 5 5 10 30 5",
        )
        self.assertAlmostEqual(stats.total, 90 / 160)
        self.assertAlmostEqual(stats.user, 50 / 160)
        self.assertAlmostEqual(stats.system, 30 / 160)
        self.assertAlmostEqual(stats.iowait, 20 / 160)
        self.assertAlmostEqual(stats.steal, 10 / 160)
        self.assertEqual(stats.online_cores, 1)
        self.assertEqual(stats.cores[0][0], "cpu7")

    def test_wait_can_be_high_when_cpu_is_idle(self):
        stats = self.cpu_samples("cpu 0 0 0 100 0", "cpu 0 0 0 150 50")
        self.assertEqual(stats.total, 0)
        self.assertEqual(stats.iowait, 0.5)

    def test_old_cpu_fields_and_counter_reset(self):
        stats = self.cpu_samples("cpu 0 0 0 0", "cpu 10 0 5 85")
        self.assertEqual(stats.total, 0.15)
        reset = self.cpu_samples("cpu 10 0 5 85", "cpu 0 0 0 0")
        self.assertEqual(reset.total, 0)

    def test_unreadable_cpu_is_not_idle(self):
        with patch("sysmon.sources.cpu._read_text", return_value=""):
            with self.assertRaises(OSError):
                CPUSource().sample()

    def test_memory_missing_optional_fields_and_shared_cache(self):
        content = "MemTotal: 1024 kB\nMemFree: 128 kB\nMemAvailable: 512 kB\nCached: 256 kB\nSReclaimable: 64 kB\nShmem: 32 kB\nBuffers: 32 kB\n"
        with patch("builtins.open", mock_open(read_data=content)):
            stats = MemSource().sample()
        self.assertEqual(stats.used, 512 * 1024)
        self.assertEqual(stats.cached, 288 * 1024)
        self.assertFalse(stats.available_estimated)
        self.assertEqual(
            (stats.swap_total, stats.swap_used, stats.slab, stats.dirty), (0, 0, 0, 0)
        )
        with patch(
            "builtins.open",
            mock_open(read_data=content.replace("MemAvailable: 512 kB\n", "")),
        ):
            old = MemSource().sample()
            self.assertEqual(old.available, 448 * 1024)
            self.assertTrue(old.available_estimated)

    def test_memory_unavailable_is_not_healthy(self):
        with patch("builtins.open", mock_open(read_data="")):
            with self.assertRaises(OSError):
                MemSource().sample()

    def test_pid_reuse_does_not_inherit_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            (root / "stat").write_text("btime 100\n")

            def write(start, ticks):
                fields = ["0"] * 22
                fields[0], fields[11], fields[17], fields[19], fields[21] = (
                    "S",
                    str(ticks),
                    "1",
                    str(start),
                    "10",
                )
                (root / "123" / "stat").write_text(
                    "123 (a weird ) name) " + " ".join(fields)
                )

            with (
                patch("sysmon.sources.procs.PROC", directory),
                patch("sysmon.sources.procs.time.monotonic", side_effect=[1, 2]),
            ):
                source = ProcSource()
                write(10, 10)
                source.sample()
                write(20, 500)
                self.assertEqual(source.sample()[123].cpu_pct, 0)

    def test_network_native_endianness(self):
        ipv4 = f"{struct.unpack('=I', socket.inet_aton('127.0.0.1'))[0]:08X}"
        self.assertEqual(_decode_addr(ipv4, False), "127.0.0.1")
        words = struct.unpack("=4I", socket.inet_pton(socket.AF_INET6, "2001:db8::1"))
        self.assertEqual(
            _decode_addr("".join(f"{word:08X}" for word in words), True), "2001:db8::1"
        )

    def test_network_reset_and_new_errors(self):
        source = NetSource(ProcSource())

        def counters(rx, errors):
            return "headers\nheaders\neth0: " + " ".join(
                map(str, [rx, 5, errors, 0, 0, 0, 0, 0, 100, 5, 0, 0, 0, 0, 0, 0])
            )

        with (
            patch("builtins.open", mock_open(read_data=counters(1000, 2000))),
            patch("sysmon.sources.net.time.monotonic", return_value=1),
        ):
            first = source._interfaces()[0]
        self.assertEqual(first.rx_error_delta, 0)
        with (
            patch("builtins.open", mock_open(read_data=counters(100, 2003))),
            patch("sysmon.sources.net.time.monotonic", return_value=2),
        ):
            second = source._interfaces()[0]
        self.assertEqual(second.rx_rate, 0)
        self.assertEqual(second.rx_error_delta, 3)


class GPUs(unittest.TestCase):
    def test_nvml_size_query_and_unknown_memory(self):
        nvml = NVML.__new__(NVML)
        nvml._lib, nvml._handles, nvml.procs_supported = (
            object(),
            [ctypes.c_void_p(1)],
            True,
        )

        def compute(handle, count, entries):
            pointer = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint))
            pointer[0] = 2
            if entries is None:
                return NVML_ERROR_INSUFFICIENT_SIZE
            entries[0].pid, entries[0].used_gpu_memory = 123, 2**64 - 1
            entries[1].pid, entries[1].used_gpu_memory = 456, 0
            return 0

        def graphics(handle, count, entries):
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint))[0] = 0
            return 0

        with patch.object(
            nvml,
            "_proc_function",
            side_effect=lambda base: compute if base == "Compute" else graphics,
        ):
            processes = nvml.processes()
        self.assertEqual([p.pid for p in processes], [123, 456])
        self.assertIsNone(processes[0].mem_bytes)
        self.assertEqual(processes[1].mem_bytes, 0)
        self.assertTrue(nvml.procs_supported)

    def test_nvml_unsupported_is_cached(self):
        nvml = NVML.__new__(NVML)
        nvml._lib, nvml._handles, nvml.procs_supported = (
            object(),
            [ctypes.c_void_p(1)],
            True,
        )
        with patch.object(
            nvml, "_proc_function", return_value=lambda *args: NVML_ERROR_NOT_SUPPORTED
        ):
            self.assertIsNone(nvml.processes())
        self.assertFalse(nvml.procs_supported)

    def test_nvml_compute_graphics_duplicate_is_one_process(self):
        nvml = NVML.__new__(NVML)
        nvml._lib, nvml._handles, nvml.procs_supported = (
            object(),
            [ctypes.c_void_p(1)],
            True,
        )

        def query(handle, count, entries):
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint))[0] = 1
            if entries is None:
                return NVML_ERROR_INSUFFICIENT_SIZE
            entries[0].pid, entries[0].used_gpu_memory = 123, 1024
            return 0

        with patch.object(nvml, "_proc_function", return_value=query):
            processes = nvml.processes()
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].kind, "C+G")
        self.assertEqual(processes[0].mem_bytes, 1024)

    def test_pmon_old_and_new_headers(self):
        for content in (
            "# gpu pid type sm mem enc dec fb command\n0 123 C 25 - 0 0 128 app\n",
            "# gpu pid type sm mem enc dec jpg ofa fb ccpm command\n0 123 C 25 - 0 0 0 0 128 0 app\n",
        ):
            with patch(
                "sysmon.sources.gpu.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=content),
            ):
                rows = _pmon_processes(1)
            self.assertEqual(rows[0].mem_bytes, 128 * 1024**2)
            self.assertEqual(rows[0].sm_pct, 0.25)
            self.assertIsNone(rows[0].mem_pct)
        with patch(
            "sysmon.sources.gpu.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout=""),
        ):
            self.assertIsNone(_pmon_processes(1))

    def test_drm_amd_intel_and_missing_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, vendor in enumerate(("0x1002", "0x8086")):
                device = root / f"card{index}" / "device"
                device.mkdir(parents=True)
                (device / "vendor").write_text(vendor)
                (device / "device").write_text("0xabcd")
            device = root / "card0" / "device"
            for name, value in (
                ("gpu_busy_percent", 42),
                ("mem_info_vram_used", 1024),
                ("mem_info_vram_total", 4096),
            ):
                (device / name).write_text(str(value))
            hwmon = device / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "temp1_input").write_text("62000")
            (hwmon / "power1_average").write_text("15000000")
            rows = drm.devices(root)
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                (rows[0].util_gpu, rows[0].temperature, rows[0].power_w), (42, 62, 15)
            )
            self.assertIsNone(rows[1].util_gpu)
            self.assertIsNone(rows[1].mem_total)
            self.assertEqual(drm.devices(root / "missing"), [])

    def test_no_gpu_requires_no_vendor_command(self):
        with (
            patch("sysmon.sources.gpu.NVML") as native,
            patch("sysmon.sources.gpu.drm.devices", return_value=[]),
            patch("sysmon.sources.gpu.subprocess.run") as command,
        ):
            native.return_value.available = False
            snapshot = GPUSource(ProcSource()).sample()
        self.assertEqual(snapshot.devices, ())
        command.assert_not_called()


class Layout(unittest.TestCase):
    def test_overview_real_widths_and_unknown_gauges(self):
        self.assertNotEqual(bar_cells(None, 20).plain, bar_cells(0, 20).plain)
        panel = OverviewPanel()
        summary = panel.fetch().summary
        for width in (40, 60, 76, 96, 116, 146, 196):
            stream = io.StringIO()
            console = Console(width=width, file=stream, no_color=True)
            console.print(panel._view(summary, width=width))
            output = stream.getvalue()
            for section in ("cpu", "memory", "gpu", "network", "top cpu", "top memory"):
                self.assertIn(section, output.lower(), (width, output))
            self.assertIn("% busy", output)
            self.assertIn("% used", output)
            self.assertNotIn("█", output)


if __name__ == "__main__":
    unittest.main()
