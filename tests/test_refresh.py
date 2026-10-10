"""Changing samples must not move overview cards or delay user actions."""

from dataclasses import replace
import unittest

from rich.console import Console
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from sysmon.app import SysmonApp
from sysmon.panels.overview import OverviewPanel, OverviewSnapshot
from sysmon.screens import ViewerScreen
from sysmon.sources.summary import Alert
import test_ui
from test_ui import fixture_sources, sample_summary


def changing_samples():
    summary, _ = sample_summary()
    yield summary
    yield replace(
        summary,
        top_cpu=(),
        top_memory=(),
        busy_interfaces=(),
        gpu=replace(summary.gpu, devices=()),
        disk=None,
        resources=None,
    )
    yield replace(
        summary,
        alerts=tuple(Alert(2, f"Threshold notice {i}") for i in range(10)),
        alert_severity=2,
        memory=replace(summary.memory, available_estimated=True),
        gpu=replace(
            summary.gpu,
            devices=tuple(replace(summary.gpu.devices[0], index=i) for i in range(12)),
        ),
        busy_interfaces=tuple(
            replace(summary.busy_interfaces[0], name=f"接口{i}" + "x" * 100)
            for i in range(4)
        ),
        net=replace(summary.net, error="permission denied"),
    )


class OverviewGeometry(unittest.TestCase):
    def test_sample_changes_preserve_height_and_card_positions(self):
        panel = OverviewPanel()
        for width in (40, 69, 70, 76, 109, 110, 116, 145, 146, 196):
            expected = None
            for history in ([], [0.1, 0.9]):
                panel.cpu_history = panel.memory_history = history
                for summary in changing_samples():
                    console = Console(width=width, no_color=True)
                    with console.capture() as output:
                        console.print(panel._view(summary, width))
                    lines = output.get().splitlines()
                    geometry = (
                        len(lines),
                        tuple(
                            (i, line.count("╭"), line.count("╰"))
                            for i, line in enumerate(lines)
                            if "╭" in line or "╰" in line
                        ),
                    )
                    with self.subTest(
                        width=width, history=bool(history), alerts=len(summary.alerts)
                    ):
                        if expected is None:
                            expected = geometry
                        self.assertEqual(geometry, expected)

    def test_overflow_links_and_estimated_memory_remain_visible(self):
        panel = OverviewPanel()
        panel.memory_history = [0.5]
        summary = list(changing_samples())[-1]
        console = Console(width=146, no_color=True)
        with console.capture() as output:
            console.print(panel._view(summary, 146))
        content = output.get()
        self.assertIn("more", content)
        self.assertIn("[n]", content)
        self.assertIn("[4]", content)
        self.assertIn("Availability estimated", content)
        self.assertIn("0–100%", content)


class RefreshInteraction(unittest.IsolatedAsyncioTestCase):
    wait_until = test_ui.Interaction.wait_until

    async def test_overview_refresh_preserves_scroll_and_extent(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait_until(
                    pilot, lambda: app.current.last_summary is not None
                )
                panel = app.current
                panel.deactivate()
                body = app.query_one("#overview-scroll", VerticalScroll)
                for width in (80, 114, 150):
                    await pilot.resize_terminal(width, 24)
                    panel.apply(OverviewSnapshot(next(changing_samples())))
                    await pilot.pause()
                    body.scroll_end(animate=False)
                    await pilot.pause()
                    geometry = body.virtual_size, body.max_scroll_y, body.scroll_y
                    for summary in changing_samples():
                        panel.apply(OverviewSnapshot(summary))
                        await pilot.pause()
                        self.assertEqual(
                            (body.virtual_size, body.max_scroll_y, body.scroll_y),
                            geometry,
                        )
                        self.assertEqual(body.scroll_x, 0)
                        self.assertEqual(body.max_scroll_x, 0)
                await pilot.press("n")
                await pilot.pause()
                self.assertIsInstance(app.screen, ViewerScreen)
                content = str(app.screen.query_one("#viewer-content", Static).content)
                for i in range(10):
                    self.assertIn(f"Threshold notice {i}", content)

    async def test_sort_and_filter_apply_without_another_sample(self):
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
                panel.sort_by("pid")
                self.assertEqual(str(table.get_row_at(0)[0]), "1240")
                panel.sort_by("pid")
                self.assertEqual(str(table.get_row_at(0)[0]), "1640")
                panel.apply_filter("compiler")
                self.assertEqual(table.row_count, 1)
                self.assertEqual(str(table.get_row_at(0)[0]), "1302")
                panel.apply_filter("")
                self.assertEqual(table.row_count, 5)

    async def test_gpu_kind_toggle_applies_without_another_sample(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.wait_until(
                    pilot, lambda: app.current.last_summary is not None
                )
                await pilot.press("4")
                panel = app.current
                await self.wait_until(pilot, lambda: panel._latest is not None)
                panel.deactivate()
                summary, _ = sample_summary()
                panel.apply(
                    replace(
                        summary.gpu,
                        processes=(
                            summary.gpu.processes[0],
                            replace(summary.gpu.processes[1], kind="G"),
                        ),
                    )
                )
                table = panel.query_one("#gpu-procs", DataTable)
                self.assertEqual(table.row_count, 2)
                await pilot.press("g")
                self.assertEqual(table.row_count, 1)
                await pilot.press("g")
                self.assertEqual(table.row_count, 2)
