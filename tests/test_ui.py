"""Headless interaction tests using safe, reproducible display data."""

from __future__ import annotations

import unittest
import time
from dataclasses import replace
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from textual.widgets import DataTable
from textual.containers import VerticalScroll

from sysmon.app import SysmonApp
from sysmon.core.context import SOURCES
from sysmon.panels.overview import OverviewPanel, OverviewSnapshot
from sysmon.panels.cpu import CPUPanel
from sysmon.panels.memory import MemoryPanel
from sysmon.panels.processes import ProcessesPanel
from sysmon.panels.network import NetworkPanel
from sysmon.panels.gpu import GPUPanel
from sysmon.sources.cpu import CPUStats
from sysmon.sources.memory import MemStats
from sysmon.sources.gpu import GPUSnapshot
from sysmon.sources.net import IfaceStats, NetSnapshot
from sysmon.sources.nvml import GPUDevice, GPUProcess
from sysmon.sources.procs import ProcInfo
from sysmon.sources.disk import Disk, DiskSnapshot, Filesystem
from sysmon.sources.pressure import Pressure, PressureLine
from sysmon.sources.resources import ResourceSnapshot, CgroupStats
from sysmon.panels.disk import DiskPanel
from sysmon.panels.resources import ResourcesPanel
from sysmon.sources.summary import SystemSummary, top_by_cpu, top_by_memory


def sample_summary() -> tuple[SystemSummary, dict[int, ProcInfo]]:
    cpu = CPUStats(
        0.374,
        0.28,
        0.084,
        0.01,
        0.01,
        tuple((f"cpu{i}", (i + 1) / 20) for i in range(16)),
        3.2,
        2.8,
        2.4,
        3200,
        "Example Linux CPU",
        16,
    )
    memory = MemStats(
        32 << 30,
        20 << 30,
        4 << 30,
        10 << 30,
        1 << 30,
        2 << 30,
        12 << 30,
        8 << 30,
        1 << 30,
        16 << 20,
    )
    processes = {
        index: ProcInfo(
            index,
            1,
            name,
            "R",
            cpu_pct,
            rss,
            4,
            120,
            1791504000,
            3600,
            "user",
            name + " --example",
        )
        for index, name, cpu_pct, rss in (
            (1240, "python", 128.4, 4 << 30),
            (1302, "compiler", 48.6, 980 << 20),
            (1450, "browser", 12.3, 1 << 30),
            (1520, "editor", 4.2, 320 << 20),
            (1640, "terminal", 1.6, 84 << 20),
        )
    }
    devices = tuple(
        GPUDevice(
            i,
            "Example NVIDIA GPU",
            util,
            None,
            used << 30,
            16 << 30,
            temp,
            70,
            230,
            35,
            900,
            6000,
        )
        for i, util, used, temp in (
            (0, 72, 8, 61),
            (1, 38, 4, 55),
            (2, 0, 0, 34),
            (3, None, 1, 40),
        )
    )
    gpu = GPUSnapshot(
        devices,
        (GPUProcess(1240, 0, "C", 4 << 30), GPUProcess(1302, 1, "C", 2 << 30)),
        processes,
        "nvml",
        "example",
        None,
        1791507600,
    )
    iface = IfaceStats(
        "eth0", 30 << 30, 8 << 30, 4.2 * 2**20, 680 * 2**10, 1000, 250, 0, 0
    )
    net = NetSnapshot((iface,), (), False, None)
    pressure = tuple(
        Pressure(name, PressureLine(value, value / 2, value / 4, 1000))
        for name, value in (("cpu", 2.5), ("memory", 0.2), ("io", 1.1))
    )
    resources = ResourceSnapshot(
        864123,
        pressure,
        CgroupStats("v2", "/example/job", 4, 4, 38, 2 << 30, 8 << 30, 8 << 30),
    )
    disk = DiskSnapshot(
        (Disk("nvme0n1", 2 << 20, 1 << 20, 80, 50, 0.12, 1, False),),
        (
            Filesystem(
                "/",
                "/dev/nvme0n1p1",
                "ext4",
                100 << 30,
                40 << 30,
                55 << 30,
                100000,
                20000,
            ),
        ),
    )
    return SystemSummary(
        cpu,
        memory,
        gpu,
        net,
        tuple(top_by_cpu(processes)),
        tuple(top_by_memory(processes)),
        (),
        (iface,),
        process_count=128,
        collected_at=1791507600,
        resources=resources,
        disk=disk,
    ), processes


@contextmanager
def fixture_sources():
    summary, processes = sample_summary()
    previous_warm = SOURCES._warmed
    with ExitStack() as stack:

        async def example_uptime(app):
            app._set_uptime(864123)

        stack.enter_context(patch.object(SysmonApp, "_uptime", example_uptime))
        stack.enter_context(
            patch.object(
                SOURCES, "warm", side_effect=lambda: setattr(SOURCES, "_warmed", True)
            )
        )
        for cls, value in (
            (OverviewPanel, OverviewSnapshot(summary)),
            (CPUPanel, summary.cpu),
            (MemoryPanel, summary.memory),
            (ProcessesPanel, processes),
            (GPUPanel, summary.gpu),
            (NetworkPanel, summary.net),
            (DiskPanel, summary.disk),
            (ResourcesPanel, summary.resources),
        ):
            stack.enter_context(patch.object(cls, "fetch", return_value=value))
        try:
            yield summary
        finally:
            SOURCES._warmed = previous_warm


class Interaction(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, pilot, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            await pilot.pause(0.1)
        self.assertTrue(condition(), "UI did not reach the expected rendered state")

    async def test_new_process_is_visible_and_selection_survives_sort(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.wait_until(
                    pilot, lambda: app.current.last_summary is not None
                )
                await pilot.press("3")
                panel = app.current
                table = panel.query_one("#proc-table", DataTable)
                await self.wait_until(pilot, lambda: table.row_count == 5)
                panel.deactivate()
                table.move_cursor(row=2)
                selected_pid = str(table.get_row_at(2)[0])
                _, processes = sample_summary()
                processes[2000] = replace(processes[1240], pid=2000, name="new job")
                panel._last_resort = time.monotonic()
                panel.apply(processes)
                self.assertEqual(table.row_count, 6)
                table.scroll_to(x=20, animate=False)
                await pilot.pause()
                previous_scroll = table.scroll_x
                rows = panel._sync._rendered.copy()
                new_order = list(reversed(panel._sync.order))
                panel._sync.sync(rows, new_order)
                await pilot.pause()
                self.assertEqual(
                    str(table.get_row_at(table.cursor_coordinate.row)[0]), selected_pid
                )
                self.assertEqual(table.scroll_x, previous_scroll)

    async def test_scroll_resize_and_wide_tables(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.5)
                body = app.query_one("#overview-scroll", VerticalScroll)
                await self.wait_until(pilot, lambda: body.max_scroll_y > 0)
                self.assertGreater(body.max_scroll_y, 0)
                await pilot.press("end")
                await pilot.pause()
                self.assertGreater(body.scroll_y, 0)
                await pilot.press("home")
                await pilot.pause()
                self.assertEqual(body.scroll_y, 0)
                await pilot.resize_terminal(150, 40)
                await pilot.pause()
                self.assertEqual(app.current._error, None)
                self.assertGreater(body.size.width, 100)
                await pilot.press("3")
                await pilot.pause(0.2)
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                table = app.query_one("#proc-table", DataTable)
                await self.wait_until(pilot, lambda: table.row_count == 5)
                self.assertGreater(table.max_scroll_x, 0)
                await pilot.press("shift+right")
                await pilot.pause()
                self.assertGreater(table.scroll_x, 0)
                self.assertEqual(app.current.KEY, "3")
                await pilot.press("4")
                await self.wait_until(pilot, lambda: app.current._latest is not None)
                cards = app.query_one("#gpu-scroll", VerticalScroll)
                await self.wait_until(pilot, lambda: cards.max_scroll_y > 0)
                self.assertGreater(
                    app.query_one("#gpu-procs", DataTable).region.height, 0
                )
                cards.focus()
                await pilot.press("end")
                await pilot.pause()
                self.assertGreater(cards.scroll_y, 0)

    async def test_digits_and_arrows_can_be_typed_in_filter(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.wait_until(
                    pilot, lambda: app.current.last_summary is not None
                )
                await pilot.press("3", "slash", "1", "2", "4", "0")
                await pilot.pause()
                self.assertEqual(app.current.KEY, "3")
                field = app.query_one("#proc-filter")
                self.assertEqual(field.value, "1240")
                await pilot.press("left", "enter")
                await pilot.pause()
                self.assertEqual(app.current.filter_text, "1240")
                await pilot.press("right")
                await pilot.pause()
                self.assertEqual(app.current.KEY, "4")


if __name__ == "__main__":
    unittest.main()
