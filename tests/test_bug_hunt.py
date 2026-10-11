"""Failures found by the extended audit: malformed data and changing identities."""

import contextlib
import ctypes
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from textual.widgets import Button, DataTable, Input, Select, Static

from sysmon.__main__ import main
from sysmon.app import SysmonApp
from sysmon.config import config_path, validate
from sysmon.history import History
from sysmon.screens import ExportScreen, HistoryScreen
from sysmon.sources.cpu import CPUSource
from sysmon.sources.net import NetSource
from sysmon.sources.nvml import NVML
from sysmon.sources.procs import ProcSource, CLOCK_TICKS, _boot_time
from sysmon.sources.resources import ResourceSource
import test_features
import test_ui
from test_ui import fixture_sources, sample_summary


class AuditMetrics(unittest.TestCase):
    def test_invalid_uptime_stays_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("nan", "inf", "-1", ""):
                with self.subTest(value=value):
                    (root / "uptime").write_text(value)
                    result = ResourceSource(root).sample()
                    self.assertIsNone(result.uptime)
                    self.assertIsNotNone(result.uptime_error)
                    json.dumps(result.uptime, allow_nan=False)

    def test_bad_numeric_settings_raise_value_error(self):
        for field in ("refresh_interval", "gpu_hot"):
            for value in (None, True, [], {}, 10**400):
                raw = (
                    {field: value}
                    if field == "refresh_interval"
                    else {"thresholds": {field: value}}
                )
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    validate(raw)

    def test_cli_large_config_number_is_a_normal_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"refresh_interval": 10**400}))
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(["--config", str(path)]), 2)
            self.assertIn("refresh interval", error.getvalue())

    def test_cli_unknown_home_in_export_path_is_a_normal_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    Path,
                    "expanduser",
                    side_effect=RuntimeError("Could not determine home directory"),
                ),
                patch("sysmon.core.context.SOURCES.warm") as warm,
            ):
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(
                        main(
                            [
                                "--config",
                                str(Path(directory) / "absent"),
                                "--export",
                                "~/report.json",
                            ]
                        ),
                        2,
                    )
                warm.assert_not_called()
                self.assertIn("home directory", error.getvalue())

    def test_empty_or_relative_xdg_uses_home_config(self):
        with patch("sysmon.config.Path.home", return_value=Path("/home/example")):
            for value in ("", "relative/path"):
                with (
                    self.subTest(value=value),
                    patch.dict("os.environ", {"XDG_CONFIG_HOME": value}),
                ):
                    self.assertEqual(
                        config_path(), Path("/home/example/.config/sysmon/config.json")
                    )

    def test_cpu_truncated_aggregate_is_unavailable(self):
        for value in ("cpu", "cpu 1 2", "cpu -1 0 0 1", "cpu a 0 0 1"):
            with (
                self.subTest(value=value),
                patch("sysmon.sources.cpu._read_text", return_value=value),
            ):
                with self.assertRaises(OSError):
                    CPUSource().sample()

    def test_cpu_blank_lines_and_malformed_core_do_not_hide_good_counters(self):
        raw = "\ncpu 10 0 5 85\n\ncpu0 malformed\ncpu7 10 0 5 85\nintr 0\n"
        with patch("sysmon.sources.cpu._read_text", return_value=raw):
            stats = CPUSource().sample()
        self.assertEqual([name for name, _ in stats.cores], ["cpu7"])

    def test_malformed_interface_row_does_not_hide_valid_interface(self):
        raw = "header\nheader\nbad: " + "oops " * 16 + "\neth0: " + "1 " * 16 + "\n"
        source = NetSource(ProcSource())
        with patch("builtins.open", mock_open(read_data=raw)):
            result = source._interfaces()
        self.assertEqual([item.name for item in result], ["eth0"])
        self.assertIsNotNone(source._iface_error)

    def test_malformed_socket_row_does_not_hide_valid_socket(self):
        raw = "header\n0: bad-address 00000000:0000 0A 0 0 0 0 0 42\n1: 0100007F:0050 00000000:0000 0A 0 0 0 0 0 43\n"
        with (
            patch("builtins.open", mock_open(read_data=raw)),
            patch("sysmon.sources.net._socket_owners", return_value={}),
        ):
            result = NetSource(ProcSource())._conns_proc()
        self.assertEqual(len(result), 4)

    def test_removed_devices_and_failed_snapshots_leave_history_gaps(self):
        summary, _ = sample_summary()
        for key, first, empty, name in (
            ("4", summary.gpu, replace(summary.gpu, devices=()), "GPU 0 busy"),
            (
                "5",
                summary.net,
                replace(summary.net, ifaces=(), error="denied"),
                "eth0 RX",
            ),
            (
                "6",
                summary.disk,
                replace(summary.disk, devices=(), error="denied"),
                "nvme0n1 read",
            ),
        ):
            with self.subTest(key=key):
                history = History()
                history.record(key, first)
                history.record(key, empty)
                self.assertEqual(len(history.series[name]), 2)
                self.assertIsNone(history.series[name][-1].value)
                history.record(key, first)
                self.assertEqual(len(history.series[name]), 3)
                self.assertIsNotNone(history.series[name][-1].value)

    def test_overview_missing_optional_sources_leave_gaps(self):
        summary, _ = sample_summary()
        history = History()
        history.record("0", SimpleNamespace(summary=summary))
        history.record(
            "0", SimpleNamespace(summary=replace(summary, disk=None, resources=None))
        )
        self.assertEqual(len(history.series["nvme0n1 read"]), 2)
        self.assertIsNone(history.series["nvme0n1 read"][-1].value)
        self.assertEqual(len(history.series["Host CPU busy"]), 2)

    def test_nvml_uses_the_stable_process_abi(self):
        nvml = NVML.__new__(NVML)
        stable, incompatible = Mock(), Mock()
        nvml._lib = SimpleNamespace(
            nvmlDeviceGetComputeRunningProcesses_v2=stable,
            nvmlDeviceGetComputeRunningProcesses_v3=incompatible,
        )
        self.assertIs(nvml._proc_function("Compute"), stable)
        nvml._lib = SimpleNamespace(
            nvmlDeviceGetComputeRunningProcesses_v3=incompatible
        )
        self.assertIsNone(nvml._proc_function("Compute"))

    def test_empty_cgroup_limit_is_unknown_with_explicit_error(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = test_features.Features.cgroup_fixture(self, directory)
            (group / "cpu.max").write_text("")
            stats = ResourceSource(proc).sample().cgroup
            self.assertIsNone(stats.cpu_capacity)
            self.assertTrue(any("cpu.max" in error for error in stats.errors))

    def test_cgroup_v1_invalid_period_is_not_reported_as_unlimited(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = test_features.Features.cgroup_fixture(self, directory)
            (proc / "self/cgroup").write_text("1:cpu,cpuacct:/job\n")
            (proc / "self/mountinfo").write_text(
                f"1 0 0:1 / {group} rw - cgroup cgroup rw,cpu,cpuacct\n"
            )
            (group / "job/cpu.cfs_quota_us").write_text("50000")
            (group / "job/cpu.cfs_period_us").write_text("0")
            (group / "job/cpuacct.usage").write_text("10000")
            stats = ResourceSource(proc).sample().cgroup
            self.assertIsNone(stats.cpu_capacity)
            self.assertTrue(any("cpu.cfs" in error for error in stats.errors))

    def test_hybrid_cgroups_honor_legacy_controller_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = test_features.Features.cgroup_fixture(self, directory)
            legacy = Path(directory) / "legacy"
            (legacy / "job").mkdir(parents=True)
            (proc / "self/cgroup").write_text("0::/job\n1:cpu,cpuacct,memory:/job\n")
            mountinfo = proc / "self/mountinfo"
            mountinfo.write_text(
                mountinfo.read_text()
                + f"2 0 0:2 / {legacy} rw - cgroup cgroup rw,cpu,cpuacct,memory\n"
            )
            for parent in (group, group / "job"):
                (parent / "cpu.max").unlink()
                (parent / "memory.max").unlink()
            (group / "job/memory.current").unlink()
            for parent in (legacy, legacy / "job"):
                (parent / "cpu.cfs_quota_us").write_text("25000")
                (parent / "cpu.cfs_period_us").write_text("100000")
                (parent / "memory.limit_in_bytes").write_text("131072")
            (legacy / "job/memory.usage_in_bytes").write_text("65536")
            (legacy / "job/cpuacct.usage").write_text("1000000000")
            source = ResourceSource(proc)
            with patch("sysmon.sources.resources.time.monotonic", return_value=1):
                stats = source.sample().cgroup
            self.assertEqual(stats.cpu_capacity, 0.25)
            self.assertEqual(stats.memory_capacity, 131072)
            self.assertEqual(stats.memory_used, 65536)
            self.assertEqual(stats.version, "hybrid")
            (legacy / "job/cpuacct.usage").write_text("1250000000")
            with patch("sysmon.sources.resources.time.monotonic", return_value=3):
                self.assertEqual(source.sample().cgroup.cpu_pct, 50)

    def test_nvml_inaccessible_device_preserves_other_gpu_indices(self):
        def count(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint))[0] = 3
            return 0

        def handle(index, pointer):
            if index == 1:
                return 4
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = index + 10
            return 0

        library = SimpleNamespace(
            nvmlInit_v2=lambda: 0,
            nvmlDeviceGetCount_v2=count,
            nvmlDeviceGetHandleByIndex_v2=handle,
        )
        with patch("sysmon.sources.nvml.ctypes.CDLL", return_value=library):
            nvml = NVML()
        self.assertEqual([device.index for device in nvml.devices()], [0, 2])

    def test_nvml_mig_allocations_sum_without_double_counting_graphics(self):
        nvml = NVML.__new__(NVML)
        nvml._lib, nvml._handles, nvml.procs_supported = (
            object(),
            [(0, ctypes.c_void_p(1))],
            True,
        )

        def query(kind):
            def collect(handle, count, entries):
                total = 2 if kind == "Compute" else 1
                ctypes.cast(count, ctypes.POINTER(ctypes.c_uint))[0] = total
                if entries is None:
                    return 7
                for slot in range(total):
                    entries[slot].pid = 123
                    entries[slot].used_gpu_memory = (slot + 1) * 1024
                    entries[slot].gpu_instance_id = slot
                    entries[slot].compute_instance_id = slot
                return 0

            return collect

        with patch.object(nvml, "_proc_function", side_effect=query):
            processes = nvml.processes()
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].mem_bytes, 3072)
        self.assertEqual(processes[0].kind, "C+G")

    def test_cgroup_mount_selection_uses_a_mount_containing_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = test_features.Features.cgroup_fixture(self, directory)
            irrelevant = Path(directory) / "irrelevant"
            irrelevant.mkdir()
            mountinfo = proc / "self/mountinfo"
            mountinfo.write_text(
                f"2 0 0:2 /other {irrelevant} rw - cgroup2 cgroup rw\n"
                + mountinfo.read_text()
            )
            stats = ResourceSource(proc).sample().cgroup
            self.assertEqual(stats.cpu_capacity, 0.5)
            self.assertEqual(stats.memory_capacity, 524288)

    def test_failed_cgroup_location_resets_cpu_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = test_features.Features.cgroup_fixture(self, directory)
            source = ResourceSource(proc)
            with patch("sysmon.sources.resources.time.monotonic", return_value=1):
                source.sample()
            (proc / "self/cgroup").write_text("0::/../../outside\n")
            source.sample()
            (proc / "self/cgroup").write_text("0::/job\n")
            (group / "job/cpu.stat").write_text("usage_usec 2000000\n")
            with patch("sysmon.sources.resources.time.monotonic", return_value=3):
                self.assertIsNone(source.sample().cgroup.cpu_pct)

    def test_invalid_boot_time_does_not_poison_process_timestamps(self):
        for raw in ("btime nan\n", "btime inf\n", "btime -1\n"):
            with (
                self.subTest(raw=raw),
                patch("builtins.open", mock_open(read_data=raw)),
            ):
                self.assertIsNone(_boot_time())

    def test_process_description_rejects_pid_reuse_during_decoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            (root / "stat").write_text("btime 100\n")
            (root / "uptime").write_text("100\n")

            def write_stat(start):
                fields = ["0"] * 22
                fields[0], fields[17], fields[19] = "S", "1", str(start)
                (root / "123/stat").write_text("123 (job) " + " ".join(fields))

            source = ProcSource()

            def command(*args):
                write_stat(20 * CLOCK_TICKS)
                return "replacement --secret"

            with (
                patch("sysmon.sources.procs.PROC", directory),
                patch("sysmon.sources.procs._read_cmdline", side_effect=command),
            ):
                write_stat(10 * CLOCK_TICKS)
                self.assertIsNone(source.describe(123))
                self.assertNotIn(123, source._cmdlines)

    def test_process_sweep_rejects_pid_reuse_during_decoration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            (root / "stat").write_text("btime 100\n")
            (root / "uptime").write_text("100\n")

            def write_stat(start):
                fields = ["0"] * 22
                fields[0], fields[17], fields[19] = "S", "1", str(start)
                (root / "123/stat").write_text("123 (job) " + " ".join(fields))

            source = ProcSource()

            def command(*args):
                write_stat(20 * CLOCK_TICKS)
                return "replacement --secret"

            with (
                patch("sysmon.sources.procs.PROC", directory),
                patch("sysmon.sources.procs._read_cmdline", side_effect=command),
            ):
                write_stat(10 * CLOCK_TICKS)
                self.assertNotIn(123, source.sample())
                self.assertNotIn(123, source._ticks)


class AuditInteraction(unittest.IsolatedAsyncioTestCase):
    wait_until = test_ui.Interaction.wait_until

    async def test_incremental_cell_growth_updates_auto_column_width(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("3")
                panel = app.current
                table = panel.query_one("#proc-table", DataTable)
                await self.wait_until(pilot, lambda: table.row_count == 5)
                panel.deactivate()
                _, processes = sample_summary()
                panel._last_resort = float("inf")
                panel.apply(processes)
                await pilot.pause()
                column = next(
                    column
                    for key, column in table.columns.items()
                    if key.value == "cmd"
                )
                previous = column.content_width
                command = "python " + "x" * 200
                panel.apply(
                    {
                        pid: replace(info, cmdline=command)
                        for pid, info in processes.items()
                    }
                )
                await pilot.pause()
                self.assertGreater(column.content_width, previous)
                self.assertGreaterEqual(column.content_width, len(command))

    async def test_open_history_tracks_new_metrics(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.wait_until(pilot, lambda: bool(app.history.series))
                app.current.deactivate()
                app.history.series.clear()
                app.history.units.clear()
                await pilot.press("H")
                self.assertIsInstance(app.screen, HistoryScreen)
                select = app.screen.query_one("#history-metric", Select)
                self.assertIs(select.value, Select.NULL)
                app.history.add("New interface RX", 123, "B/s")
                app.screen._redraw()
                await pilot.pause()
                self.assertEqual(select.value, "New interface RX")
                app.history.add("Host CPU busy", 25)
                app.screen._redraw()
                await pilot.pause()
                self.assertEqual(select.value, "New interface RX")
                select.value = "Host CPU busy"
                await pilot.pause()
                self.assertEqual(select.value, "Host CPU busy")

    async def test_export_path_error_keeps_dialog_usable(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.wait_until(pilot, lambda: bool(app.history.series))
                await pilot.press("e")
                self.assertIsInstance(app.screen, ExportScreen)
                app.screen.query_one("#export-path", Input).value = "~/report.json"
                with patch.object(
                    Path,
                    "expanduser",
                    side_effect=RuntimeError("Could not determine home directory"),
                ):
                    await pilot.click("#export-save")
                self.assertIsInstance(app.screen, ExportScreen)
                self.assertIn(
                    "home directory",
                    app.screen.query_one("#export-message", Static).content.plain,
                )
                self.assertFalse(app.screen.query_one("#export-save", Button).disabled)
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)
