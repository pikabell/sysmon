"""Export reproducible overview previews without exposing host process data."""

import asyncio
from pathlib import Path

from sysmon.app import SysmonApp
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
                path = save_preview(app, f"overview-{width}.svg", target)
                print(path)
                if width == 150:
                    for key, name in (("4", "gpu"), ("6", "disk"), ("7", "resources")):
                        await pilot.press(key)
                        await pilot.pause(0.3)
                        print(save_preview(app, f"{name}.svg", target))
                    await pilot.press("H")
                    await pilot.pause(0.3)
                    print(save_preview(app, "history.svg", target))


if __name__ == "__main__":
    asyncio.run(main())
