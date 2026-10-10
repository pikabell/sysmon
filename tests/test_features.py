"""Metric edge cases and real user paths for the public-release feature set."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Button, DataTable, Input, Static

from sysmon.app import SysmonApp
from sysmon.config import Settings, load, save, validate
from sysmon.export import snapshot, write_report
from sysmon.history import History, plot
from sysmon.screens import (
    ExportScreen,
    HistoryScreen,
    ProcessScreen,
    SettingsScreen,
    ViewerScreen,
)
from sysmon.sources.details import ProcessDetails, inspect_process
from sysmon.sources.disk import DiskSource, DiskSnapshot
from sysmon.sources.pressure import pressure_snapshot, read_pressure
from sysmon.sources.procs import ProcSource
from sysmon.sources.resources import (
    CgroupStats,
    ResourceSnapshot,
    ResourceSource,
    cpuset_count,
)
from test_ui import fixture_sources, sample_summary


class Features(unittest.TestCase):
    def test_pressure_windows_and_undefined_system_cpu_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for resource in ("cpu", "memory", "io"):
                (root / resource).write_text(
                    "some avg10=12.50 avg60=3.00 avg300=1.00 total=1234\nfull avg10=4.00 avg60=1.00 avg300=0.20 total=456\n"
                )
            pressure = pressure_snapshot(root)
            self.assertEqual(pressure[0].some.avg10, 12.5)
            self.assertIsNone(pressure[0].full)
            self.assertEqual(pressure[1].full.total_us, 456)
            self.assertEqual(read_pressure(root / "cpu", "cpu").full.avg10, 4)
            (root / "io").write_text("some avg10=nan avg60=1 avg300=2 total=0")
            self.assertIsNotNone(read_pressure(root / "io", "io").error)
            self.assertIsNotNone(read_pressure(root / "absent", "cpu").error)
        with patch.object(Path, "read_text", side_effect=PermissionError()):
            self.assertEqual(
                read_pressure(Path("/psi"), "cpu").error, "permission denied"
            )

    def test_disk_deltas_sector_size_reset_and_mount_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, sys = root / "proc", root / "sys"
            (proc / "self").mkdir(parents=True)
            (sys / "class/block/sda1").mkdir(parents=True)
            (sys / "class/block/sda1/partition").write_text("1")
            (proc / "self/mountinfo").write_text(
                f"1 0 8:1 / {directory} rw - ext4 /dev/sda1 rw\n2 0 0:1 / /mnt/remote\\040share rw - nfs server:/share rw\n"
            )
            source = DiskSource(proc, sys)
            (proc / "diskstats").write_text("8 1 sda1 10 0 100 0 20 0 200 0 1 100 0\n")
            with patch("sysmon.sources.disk.time.monotonic", return_value=1):
                first = source.sample()
            self.assertIsNone(first.devices[0].read_rate)
            self.assertTrue(first.devices[0].partition)
            self.assertGreater(first.filesystems[0].total, 0)
            self.assertEqual(first.filesystems[1].path, "/mnt/remote share")
            self.assertIn("skipped", first.filesystems[1].error)
            (proc / "diskstats").write_text("8 1 sda1 14 0 104 0 26 0 208 0 2 600 0\n")
            with patch("sysmon.sources.disk.time.monotonic", return_value=3):
                second = source.sample().devices[0]
            self.assertEqual(second.read_rate, 1024)
            self.assertEqual(second.write_rate, 2048)
            self.assertEqual(second.reads_per_second, 2)
            self.assertEqual(second.busy, 0.25)
            (proc / "diskstats").write_text("8 1 sda1 1 0 1 0 1 0 1 0 0 1 0\n")
            with patch("sysmon.sources.disk.time.monotonic", return_value=4):
                self.assertIsNone(source.sample().devices[0].read_rate)
            (proc / "diskstats").unlink()
            self.assertIsNotNone(source.sample().error)

    def test_cpuset_intervals_do_not_double_count(self):
        self.assertEqual(cpuset_count("0-3,2-5,9,9"), 7)
        self.assertEqual(cpuset_count(""), 0)
        for value in ("3-1", "a", "0-1-2"):
            with self.assertRaises(ValueError):
                cpuset_count(value)

    def cgroup_fixture(self, directory):
        root = Path(directory)
        proc, group = root / "proc", root / "group"
        (proc / "self").mkdir(parents=True)
        (group / "job").mkdir(parents=True)
        (proc / "uptime").write_text("12345.25 1234")
        (proc / "meminfo").write_text("MemTotal: 1024 kB\n")
        (proc / "self/cgroup").write_text("0::/job\n")
        (proc / "self/mountinfo").write_text(
            f"1 0 0:1 / {group} rw - cgroup2 cgroup rw\n"
        )
        (group / "cpu.max").write_text("50000 100000")
        (group / "memory.max").write_text("524288")
        (group / "job/cpu.max").write_text("max 100000")
        (group / "job/cpuset.cpus.effective").write_text("0-3")
        (group / "job/memory.max").write_text("max")
        (group / "job/memory.current").write_text("262144")
        (group / "job/cpu.stat").write_text("usage_usec 1000000\n")
        return proc, group

    def test_cgroup_v2_inherited_limits_cpu_rate_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = self.cgroup_fixture(directory)
            source = ResourceSource(proc)
            with patch("sysmon.sources.resources.time.monotonic", return_value=1):
                first = source.sample()
            self.assertEqual(first.uptime, 12345.25)
            self.assertEqual(first.cgroup.cpu_capacity, 0.5)
            self.assertEqual(first.cgroup.memory_capacity, 524288)
            self.assertEqual(first.cgroup.memory_used, 262144)
            self.assertIsNone(first.cgroup.cpu_pct)
            (group / "job/cpu.stat").write_text("usage_usec 1500000\n")
            with patch("sysmon.sources.resources.time.monotonic", return_value=3):
                self.assertEqual(source.sample().cgroup.cpu_pct, 50)
            (group / "job/cpu.stat").write_text("usage_usec 1\n")
            with patch("sysmon.sources.resources.time.monotonic", return_value=4):
                self.assertIsNone(source.sample().cgroup.cpu_pct)
            (group / "memory.max").write_text("0")
            self.assertEqual(source.sample().cgroup.memory_capacity, 0)

    def test_cgroup_v1_and_unsafe_namespace_path(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = self.cgroup_fixture(directory)
            (proc / "self/cgroup").write_text("1:cpu,cpuacct,memory,cpuset:/job\n")
            (proc / "self/mountinfo").write_text(
                f"1 0 0:1 / {group} rw - cgroup cgroup rw,cpu,cpuacct,memory,cpuset\n"
            )
            for name, value in {
                "cpu.cfs_quota_us": "25000",
                "cpu.cfs_period_us": "100000",
                "memory.limit_in_bytes": "131072",
            }.items():
                (group / name).write_text(value)
            for name, value in {
                "cpu.cfs_quota_us": "-1",
                "cpu.cfs_period_us": "100000",
                "cpuacct.usage": "1000000000",
                "memory.usage_in_bytes": "10000",
                "memory.limit_in_bytes": str(2**63 - 4096),
                "cpuset.cpus": "0-3",
            }.items():
                (group / "job" / name).write_text(value)
            stats = ResourceSource(proc).sample().cgroup
            self.assertEqual(stats.version, "v1")
            self.assertEqual(stats.cpu_capacity, 0.25)
            self.assertEqual(stats.memory_limit, 131072)
            (proc / "self/cgroup").write_text("0::/../../outside\n")
            (proc / "self/mountinfo").write_text(
                f"1 0 0:1 / {group} rw - cgroup2 cgroup rw\n"
            )
            stats = ResourceSource(proc).sample().cgroup
            self.assertIsNone(stats.cpu_capacity)
            self.assertIn("outside", stats.errors[0])

    def test_cgroup_malformed_limits_are_not_reported_as_unlimited(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, group = self.cgroup_fixture(directory)
            (group / "cpu.max").write_text("oops")
            (group / "memory.max").write_text("oops")
            stats = ResourceSource(proc).sample().cgroup
            self.assertIsNone(stats.cpu_capacity)
            self.assertIsNone(stats.memory_capacity)
            self.assertGreaterEqual(len(stats.errors), 2)

    def test_settings_roundtrip_and_rejected_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/config.json"
            self.assertEqual(load(path), Settings())
            settings = validate(
                {
                    "refresh_interval": 0.5,
                    "default_panel": "7",
                    "hidden_interfaces": ["lo", "lo"],
                    "thresholds": {"gpu_hot": 90},
                }
            )
            save(settings, path)
            self.assertEqual(load(path), settings)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("[]")
            with self.assertRaises(ValueError):
                load(path)
        for raw in (
            {"refresh_interval": float("nan")},
            {"refresh_interval": 0},
            {"default_panel": "10"},
            {"history_samples": True},
            {"hidden_interfaces": "lo"},
            {"thresholds": {"gpu_warm": 90, "gpu_hot": 80}},
            {"unexpected": 1},
        ):
            with self.assertRaises(ValueError):
                validate(raw)

    def test_history_is_bounded_and_missing_readings_are_distinct(self):
        history = History(10)
        for index in range(20):
            history.add("CPU", index)
        self.assertEqual(len(history.series["CPU"]), 10)
        history.add("CPU", None)
        rendered = plot(list(history.series["CPU"]), "%", width=20).plain
        self.assertIn("?", rendered)
        self.assertIn("0–100%", rendered)
        self.assertIn("last -", rendered)
        self.assertIn("No samples", plot([], "%").plain)
        history.limit = 5
        history.add("CPU", 20)
        self.assertEqual(len(history.series["CPU"]), 5)

    def test_export_redacts_all_command_sources_and_never_overwrites(self):
        from sysmon.sources.net import ConnInfo

        summary, procs = sample_summary()
        sources = Mock()
        sources.collect.side_effect = lambda operation: operation()
        sources.cpu.sample.return_value = summary.cpu
        sources.mem.sample.return_value = summary.memory
        sources.net.sample.return_value = replace(
            summary.net,
            conns=(
                ConnInfo(
                    "tcp", "*:80", "", "LISTEN", 1240, "example", "secret --token value"
                ),
            ),
        )
        sources.gpu.sample.return_value = replace(summary.gpu, details=procs)
        sources.resources.sample.return_value = ResourceSnapshot(1, (), CgroupStats())
        sources.disk.sample.return_value = DiskSnapshot((), ())
        sources.proc.sample.return_value = procs
        report = snapshot(sources)
        self.assertNotIn("cmdline", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))
        self.assertIn("cmdline", json.dumps(snapshot(sources, include_commands=True)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_report(report, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_report({"other": True}, path)
            self.assertEqual(
                json.loads(path.read_text()), json.loads(json.dumps(report))
            )

    def test_own_process_inspection_and_pid_reuse(self):
        source = ProcSource()
        info = source.describe(os.getpid())
        details = inspect_process(source, info)
        self.assertEqual(details.info.pid, os.getpid())
        self.assertTrue(details.status)
        self.assertTrue(details.limits)
        self.assertTrue(details.threads)
        with patch.object(
            source,
            "describe",
            return_value=replace(info, start_ticks=info.start_ticks + 10),
        ):
            with self.assertRaises(ProcessLookupError):
                inspect_process(source, info)

    def test_age_uses_uptime_and_exec_refreshes_cached_command(self):
        from sysmon.sources.procs import CLOCK_TICKS, _Raw

        source = ProcSource()
        source.boot_time = 100
        raw = _Raw(123, "old", "S", 1, 10, 1, 10 * CLOCK_TICKS, 1024)
        with patch(
            "sysmon.sources.procs._read_cmdline",
            side_effect=["old --argument", "new --argument"],
        ):
            before = source._decorate(raw, 1000, 20)
            after = source._decorate(replace(raw, name="new"), 90000, 21)
        self.assertEqual(before.age, 10)
        self.assertEqual(after.age, 11)
        self.assertEqual(after.start_ts, before.start_ts)
        self.assertEqual(after.cmdline, "new --argument")

    def test_missing_boot_time_keeps_job_timestamps_unknown(self):
        from sysmon.sources.procs import _Raw
        from sysmon.core.render import fmt_timestamp

        source = ProcSource()
        raw = _Raw(123, "job", "S", 1, 10, 1, 100, 1024)
        with patch("sysmon.sources.procs._boot_time", return_value=None):
            info = source._decorate(raw, 12345)
        self.assertIsNone(info.start_ts)
        self.assertIsNone(info.age)
        self.assertEqual(fmt_timestamp(info.start_ts), "-")
        self.assertEqual(info.start_ticks, 100)

    def test_network_command_markup_is_literal(self):
        from sysmon.panels.network import NetworkPanel
        from sysmon.sources.net import ConnInfo

        cells = NetworkPanel._conn_cells(
            ConnInfo(
                "tcp", "*:1", "", "LISTEN", 123, "user", "python [red] --example [/red]"
            )
        )
        self.assertEqual(cells[-1].plain, "python [red] --example [/red]")

    def test_hidden_interfaces_and_configured_notice_thresholds(self):
        from sysmon.config import Thresholds
        from sysmon.sources.summary import build_alerts

        summary, _ = sample_summary()
        history = History(10)
        history.hidden_interfaces = ("eth0",)
        history.record("5", summary.net)
        self.assertNotIn("eth0 RX", history.series)
        cpu = replace(summary.cpu, total=0.5, load1=100)
        self.assertFalse(
            any(
                "cpu saturated" in alert.message
                for alert in build_alerts(cpu, summary.memory, summary.gpu, summary.net)
            )
        )
        self.assertTrue(
            any(
                "cpu saturated" in alert.message
                for alert in build_alerts(
                    cpu,
                    summary.memory,
                    summary.gpu,
                    summary.net,
                    Thresholds(cpu_busy=40),
                )
            )
        )

    def test_failed_sampling_leaves_a_gap_in_history(self):
        history = History(10)
        summary, _ = sample_summary()
        history.record("0", type("Snapshot", (), {"summary": summary})())
        history.failed("1")
        self.assertIsNone(history.series["Host CPU busy"][-1].value)
        self.assertIsNotNone(history.series["Host RAM used"][-1].value)
        history.resize(1)
        self.assertTrue(all(len(values) == 1 for values in history.series.values()))

    def test_cli_errors_and_export_flags(self):
        from sysmon.__main__ import build_parser, main
        import contextlib
        import io

        arguments = build_parser().parse_args(
            [
                "--panel",
                "7",
                "--refresh",
                ".5",
                "--export",
                "/tmp/report",
                "--include-commands",
            ]
        )
        self.assertEqual(arguments.panel, "7")
        self.assertTrue(arguments.include_commands)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"theme":"missing-theme"}')
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(["--config", str(path)]), 2)
            self.assertIn("unknown theme", error.getvalue())
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(
                    main(
                        [
                            "--refresh",
                            "nan",
                            "--config",
                            str(Path(directory) / "absent"),
                        ]
                    ),
                    2,
                )
            self.assertIn("refresh interval", error.getvalue())


class FeatureInteraction(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, pilot, predicate):
        for _ in range(200):
            if predicate():
                return
            await pilot.pause(0.1)
        self.fail("expected UI state was not reached")

    async def test_every_panel_renders_and_resizes(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(80, 24)) as pilot:
                for key in "01234567":
                    await pilot.press(key)
                    panel = app.current
                    await self.wait_until(
                        pilot, lambda panel=panel: bool(panel.status_text)
                    )
                    self.assertIsNone(panel._error, (key, panel.status_text))
                    self.assertIn("sample", panel.status_text)
                    self.assertGreater(panel.region.height, 0)
                    await pilot.resize_terminal(150, 40)
                    await pilot.pause(0.1)
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause(0.1)
                    self.assertIsNone(panel._error, key)

    async def test_new_panels_help_history_and_uptime(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait_until(
                    pilot, lambda: app.current.last_summary is not None
                )
                await self.wait_until(pilot, lambda: bool(app.history.series))
                self.assertIn("uptime", app.sub_title)
                header = app.query_one("#system-header", Static)
                self.assertEqual(header.region.height, 1)
                self.assertIn("10d 00:02:03", header.content.plain)
                self.assertGreater(app.query_one("#tabs").region.y, header.region.y)
                self.assertLessEqual(app.query_one("#tab-7").region.right, 80)
                await pilot.press("H")
                self.assertIsInstance(app.screen, HistoryScreen)
                await pilot.pause()
                self.assertIn(
                    "samples",
                    app.screen.query_one("#history-plot", Static).content.plain,
                )
                await pilot.press("7")
                self.assertEqual(app.current.KEY, "0")
                await pilot.press("escape", "6")
                await self.wait_until(
                    pilot,
                    lambda: (
                        app.current.query_one("#disk-table", DataTable).row_count > 0
                    ),
                )
                self.assertIsNone(app.current._error)
                await pilot.press("i")
                self.assertIsInstance(app.screen, ViewerScreen)
                self.assertIn(
                    "512", app.screen.query_one("#viewer-content", Static).content.plain
                )
                await pilot.press("escape", "7")
                await self.wait_until(pilot, lambda: hasattr(app.current, "_latest"))
                self.assertIsNone(app.current._error)
                from rich.console import Console

                console = Console(width=80)
                with console.capture() as capture:
                    console.print(
                        app.current.query_one("#resources-body", Static).content
                    )
                self.assertIn("effective capacity", capture.get())
                await pilot.resize_terminal(50, 16)
                await pilot.pause()
                self.assertIsNone(app.current._error)
                self.assertGreaterEqual(app.query_one("#tabs").scroll_x, 0)

    async def test_settings_save_invalid_input_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory, fixture_sources():
            path = Path(directory) / "config.json"
            app = SysmonApp(settings_path=path)
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("comma")
                self.assertIsInstance(app.screen, SettingsScreen)
                field = app.screen.query_one("#setting-refresh_interval", Input)
                field.value = "0"
                button = app.screen.query_one("#settings-save", Button)
                button.scroll_visible(animate=False)
                await pilot.pause()
                await pilot.click("#settings-save")
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertFalse(path.exists())
                field.value = ".5"
                app.screen.query_one("#setting-default_panel", Input).value = "7"
                button.scroll_visible(animate=False)
                await pilot.pause()
                await self.wait_until(pilot, lambda: not button.has_class("-active"))
                self.assertTrue(await pilot.click("#settings-save"))
                await pilot.pause()
                if isinstance(app.screen, SettingsScreen):
                    self.fail(
                        str(app.screen.query_one("#settings-message", Static).content)
                    )
                self.assertEqual(app.settings.refresh_interval, 0.5)
                self.assertEqual(load(path).default_panel, "7")
                self.assertEqual(app.current.INTERVAL, 0.5)

    async def test_saved_default_panel_and_hidden_interfaces(self):
        with fixture_sources():
            app = SysmonApp(Settings(default_panel="5", hidden_interfaces=("eth0",)))
            async with app.run_test(size=(80, 24)) as pilot:
                self.assertEqual(app.current.KEY, "5")
                await self.wait_until(pilot, lambda: bool(app.current.status_text))
                self.assertEqual(
                    app.current.query_one("#net-ifaces", DataTable).row_count, 0
                )
                self.assertNotIn("eth0 RX", app.history.series)

    async def test_process_details_and_gpu_start_time(self):
        from sysmon.sources.nvml import GPUProcess

        summary, procs = sample_summary()
        details = ProcessDetails(
            procs[1240],
            (("Threads", "4"),),
            (("read_bytes", 123),),
            "Max open files 1024 1024 files",
            (("1", "/tmp/example"),),
            ((1240, "python", "R"),),
            (),
        )
        with (
            fixture_sources(),
            patch("sysmon.screens.inspect_process", return_value=details),
        ):
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("3")
                await self.wait_until(
                    pilot,
                    lambda: (
                        app.current.query_one("#proc-table", DataTable).row_count > 0
                    ),
                )
                await pilot.press("enter")
                self.assertIsInstance(app.screen, ProcessScreen)
                await self.wait_until(
                    pilot,
                    lambda: (
                        "Resource limits"
                        in app.screen.query_one("#viewer-content", Static).content.plain
                    ),
                )
                await pilot.press("escape", "4")
                await self.wait_until(pilot, lambda: app.current._latest is not None)
                panel = app.current
                panel.deactivate()
                gpu = replace(
                    summary.gpu,
                    processes=(GPUProcess(1240, 0, "C", 1024, None, None),),
                    details=procs,
                )
                panel.apply(gpu)
                table = panel.query_one("#gpu-procs", DataTable)
                start = str(table.get_row_at(0)[1])
                self.assertRegex(start, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
                await pilot.press("enter")
                self.assertIsInstance(app.screen, ProcessScreen)

    async def test_export_dialog_writes_private_report(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            fixture_sources(),
            patch(
                "sysmon.screens.snapshot",
                return_value={"schema_version": 1, "processes": []},
            ),
        ):
            path = Path(directory) / "report.json"
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.4)
                await pilot.press("e")
                self.assertIsInstance(app.screen, ExportScreen)
                app.screen.query_one("#export-path", Input).value = str(path)
                await pilot.click("#export-save")
                await self.wait_until(pilot, lambda: path.exists())
                self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
