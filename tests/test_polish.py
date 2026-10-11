"""Layout checks for compact panels, contextual controls and pinned actions."""

import unittest
from dataclasses import replace

from textual.widgets import Button, DataTable

from sysmon.app import SysmonApp
from sysmon.config import Settings
from sysmon.screens import SettingsScreen
from test_ui import fixture_sources, sample_summary
import test_ui


class Presentation(unittest.IsolatedAsyncioTestCase):
    wait_until = test_ui.Interaction.wait_until

    async def test_footer_actions_follow_the_visible_panel(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait_until(pilot, lambda: bool(app.history.series))
                self.assertFalse(app.check_action("cycle_sort", ()))
                self.assertFalse(app.check_action("toggle_gpu", ()))
                self.assertTrue(app.check_action("notices", ()))
                await pilot.press("3")
                self.assertTrue(app.check_action("cycle_sort", ()))
                self.assertTrue(app.check_action("process_details", ()))
                self.assertFalse(app.check_action("notices", ()))
                await pilot.press("4")
                self.assertTrue(app.check_action("toggle_gpu", ()))
                self.assertFalse(app.check_action("cycle_sort", ()))

    async def test_small_disk_sample_does_not_reserve_half_the_screen(self):
        with fixture_sources():
            app = SysmonApp(Settings(default_panel="6"))
            async with app.run_test(size=(80, 24)) as pilot:
                table = app.current.query_one("#disk-table", DataTable)
                await self.wait_until(pilot, lambda: table.row_count == 1)
                app.current.deactivate()
                self.assertEqual(table.region.height, 3)
                summary, _ = sample_summary()
                app.current.apply(
                    replace(
                        summary.disk,
                        devices=tuple(
                            replace(summary.disk.devices[0], name=f"sda{i}")
                            for i in range(30)
                        ),
                    )
                )
                await pilot.pause()
                self.assertLessEqual(
                    table.region.height, app.current.region.height // 2
                )
                self.assertGreater(
                    app.current.query_one("#mount-table", DataTable).region.height, 0
                )

    async def test_settings_actions_remain_visible_when_fields_scroll(self):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(50, 16)) as pilot:
                await pilot.press("comma")
                self.assertIsInstance(app.screen, SettingsScreen)
                await pilot.pause()
                save = app.screen.query_one("#settings-save", Button)
                region = save.region
                self.assertGreater(region.height, 0)
                self.assertGreater(region.y, 0)
                self.assertLessEqual(region.bottom, app.size.height)
                app.screen.query_one(".dialog-fields").scroll_end(animate=False)
                await pilot.pause()
                self.assertEqual(save.region, region)
                await pilot.press("escape")
                self.assertEqual(len(app.screen_stack), 1)

    async def test_light_theme_preserves_table_and_dialog_layout(self):
        with fixture_sources():
            app = SysmonApp(Settings(theme="textual-light", default_panel="6"))
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait_until(pilot, lambda: bool(app.current.status_text))
                self.assertIsNone(app.current._error)
                await pilot.press("comma")
                await pilot.pause()
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertGreater(
                    app.screen.query_one("#settings-save", Button).region.height, 0
                )
