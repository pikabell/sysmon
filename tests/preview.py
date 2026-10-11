"""Export reproducible overview previews without exposing host process data."""

import asyncio
import math
from pathlib import Path
from collections import deque

from sysmon.app import SysmonApp
from sysmon.history import Point
from textual.widgets import Select, Static
from rich.text import Text
from test_ui import fixture_sources


def save_preview(app, name, target):
    path = Path(app.save_screenshot(name, path=str(target)))
    path.write_text(
        "\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n"
    )
    return path


async def main():
    target = Path(__file__).resolve().parents[1] / "docs"
    target.mkdir(exist_ok=True)
    for width, height in ((150, 40), (80, 24)):
        with fixture_sources():
            app = SysmonApp()
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause(0.6)
                # Seed fixture history so the preview shows a useful trend.
                trend = [0.35 + 0.22 * math.sin(index / 6) for index in range(60)]
                app.current.cpu_history = trend[-40:]
                app.current.memory_history = [
                    0.32 + index / 1000 for index in range(40)
                ]
                app.current._redraw()
                app.history.series["Host CPU busy"] = deque(
                    (
                        Point(1791507540 + index, float(index), value * 100)
                        for index, value in enumerate(trend)
                    ),
                    maxlen=app.history.limit,
                )
                await pilot.pause(0.1)
                path = save_preview(app, f"overview-{width}.svg", target)
                print(path)
                if width == 150:
                    for key, name in (
                        ("1", "cpu"),
                        ("2", "memory"),
                        ("3", "processes"),
                        ("4", "gpu"),
                        ("5", "network"),
                        ("6", "disk"),
                        ("7", "resources"),
                    ):
                        await pilot.press(key)
                        await pilot.pause(0.3)
                        print(save_preview(app, f"{name}.svg", target))
                    await pilot.press("H")
                    await pilot.pause(0.3)
                    app.screen.query_one(
                        "#history-metric", Select
                    ).value = "Host CPU busy"
                    await pilot.pause(0.1)
                    print(save_preview(app, "history.svg", target))
                    await pilot.press("escape", "comma")
                    await pilot.pause(0.2)
                    # The displayed settings destination is fixture text too.
                    app.screen.query_one("#settings-message", Static).update(
                        Text("~/.config/sysmon/config.json")
                    )
                    print(save_preview(app, "settings.svg", target))


if __name__ == "__main__":
    asyncio.run(main())
