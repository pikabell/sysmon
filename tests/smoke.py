"""Headless smoke test: boot the app, visit every panel, dump what it rendered.

Run with:  uv run python tests/smoke.py
"""

from __future__ import annotations

import asyncio
import re
import sys

from rich.console import Console
from textual.widgets import ContentSwitcher, DataTable, Input, Static

from sysmon.app import SysmonApp
from sysmon.core.panel import Panel
from sysmon.core.registry import PANELS


def render_to_text(renderable, width: int = 140) -> str:
    """Render a Rich renderable to plain text for inspection."""
    if renderable is None:
        return ""
    if callable(renderable):
        return "<render callable>"
    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def static_text(static: Static, width: int = 140) -> str:
    """Best-effort plain text for whatever a Static is currently showing."""
    for attribute in ("content", "renderable"):
        content = getattr(static, attribute, None)
        if content is None:
            continue
        try:
            text = render_to_text(content, width)
            if text.strip():
                return text
        except Exception:
            continue
    return "<no readable content>"


def dump_panel(panel: Panel) -> None:
    print(f"\n{'=' * 100}")
    print(
        f"PANEL {panel.KEY} {panel.TITLE}   active={panel._active}  error={panel._error}"
    )
    print("=" * 100)
    for static in panel.query(Static):
        if static.id in ("status",):
            continue
        text = static_text(static)
        if text.strip():
            print(f"  --- {static.id or '?'} ---")
            for line in text.splitlines():
                if line.strip():
                    print(f"    {line.rstrip()}")
    for table in panel.query(DataTable):
        rows = table.row_count
        columns = len(table.columns)
        print(f"  --- {table.id}: {rows} rows, {columns} cols ---")
        for index in range(min(rows, 8)):
            try:
                cells = [
                    render_to_text(table.get_row_at(index)[i], width=44).replace(
                        "\n", " "
                    )
                    for i in range(columns)
                ]
            except Exception as exc:
                print(f"    <row {index} unreadable: {exc}>")
                continue
            print("      " + " | ".join(cells))
        if rows > 8:
            print(f"      ... {rows - 8} more rows")


def check_cli() -> int:
    """The CLI must answer --version/--help-keys without launching a TUI."""
    from sysmon.__main__ import build_parser

    failures = 0
    cases = (["--version"], ["--help"], ["--help-keys"], ["--panel", "4"])
    for argv in cases:
        label = " ".join(argv)
        try:
            namespace = build_parser().parse_args(argv)
        except SystemExit as exc:
            if exc.code in (0, None):
                print(f"  cli {label:<14} OK (exit {exc.code})")
            else:
                failures += 1
                print(f"  cli {label:<14} FAILED: exit {exc.code}")
            continue
        if argv == ["--help-keys"]:
            print(f"  cli {label:<14} OK")
        else:
            print(f"  cli {label:<14} OK -> panel={getattr(namespace, 'panel', None)}")
    return failures


async def check_overview(pilot) -> int:
    """The overview must land first and actually summarise the machine."""
    app = pilot.app
    switcher = app.query_one("#panels", ContentSwitcher)
    failures = 0

    print("OVERVIEW")
    landed = switcher.current == "panel-0"
    if not landed:
        failures += 1
    print(
        f"  launches on the overview: {'OK' if landed else 'FAILED'} ({switcher.current})"
    )

    panel = app.query_one("#panel-0", Panel)
    for name, text in panel.section_texts().items():
        filled = bool(text.strip())
        if not filled:
            failures += 1
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        # Strip SGR codes so the assertion output stays readable.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", first_line)
        print(f"  overview {name:<11} {'OK' if filled else 'EMPTY'}  {plain[:56]!r}")

    # A summary must not degenerate into the full table.
    summary = panel.last_summary
    bounded = (
        len(summary.top_cpu) <= 5
        and len(summary.top_memory) <= 5
        and len(summary.busy_interfaces) <= 4
    )
    if not bounded:
        failures += 1
    print(
        f"  overview stays compact: {'OK' if bounded else 'TOO MUCH'}"
        f"  {len(summary.top_cpu)} cpu / {len(summary.top_memory)} mem /"
        f" {len(summary.busy_interfaces)} ifaces of {summary.process_count} processes"
    )

    # Sources are shared, so the other panels must not have re-created them.
    from sysmon.core.context import SOURCES

    shared = panel.proc is SOURCES.proc and panel.gpu is SOURCES.gpu
    if not shared:
        failures += 1
    print(f"  overview uses shared sources: {'OK' if shared else 'FAILED'}")
    return failures


async def check_switching(pilot) -> int:
    """Panel switching must work for digits and arrows, with any widget focused."""
    app = pilot.app
    switcher = app.query_one("#panels", ContentSwitcher)
    failures = 0

    async def expect_change(label, key, before) -> None:
        nonlocal failures
        await pilot.press(key)
        await pilot.pause()
        await asyncio.sleep(0.35)
        if switcher.current == before:
            failures += 1
            print(f"  {label:<34} STUCK on {before}")
        else:
            print(f"  {label:<34} OK  {before} -> {switcher.current}")

    print("SWITCHING")
    for key in ("1", "2", "3", "4", "5", "0"):
        await expect_change(f"digit {key}", key, switcher.current)

    # Arrows must win over DataTable's own left/right cursor bindings.
    for key in ("left", "right", "left"):
        await expect_change(f"arrow {key}", key, switcher.current)

    # Each panel must receive focus, or arrows/typing would target a hidden widget.
    # Panels with a table focus their largest table (the one holding the rows the
    # user came to read); CPU and Memory have no table, so they focus the panel.
    # Panel 0 is the overview (no table), 1 and 2 too; the rest hold tables.
    expected = {
        0: "panel-0",
        1: "panel-1",
        2: "panel-2",
        3: "proc-table",
        4: "gpu-procs",
    }
    for index, want in expected.items():
        app.show_panel(index)
        await pilot.pause()
        await asyncio.sleep(0.5)
        got = app.focused.id if app.focused else None
        if got != want:
            failures += 1
        print(
            f"  focus panel {index + 1}: expected {want:<12} got {str(got):<12} {'OK' if got == want else 'MISMATCH'}"
        )

    # Network has two tables; the bigger one (connections) must win.
    app.show_panel(5)
    await pilot.pause()
    await asyncio.sleep(0.6)
    net_focus = app.focused.id if app.focused else None
    net_ok = net_focus == "net-conns"
    if not net_ok:
        failures += 1
    print(
        f"  focus panel 5: expected net-conns   got {str(net_focus):<12} {'OK' if net_ok else 'MISMATCH'}"
    )

    # The filter must own typing while focused, then hand keys back.
    # Processes is now index 3 (0=overview, 1=CPU, 2=Memory, 3=Processes...).
    app.show_panel(3)
    await pilot.pause()
    await asyncio.sleep(0.5)
    await pilot.press("slash")
    await pilot.pause()
    print(
        f"  '/' focuses filter: {'OK' if app.focused and app.focused.id == 'proc-filter' else 'FAILED'}"
    )
    for char in "python":
        await pilot.press(char)
    await pilot.press("enter")
    await pilot.pause()
    await asyncio.sleep(1.2)
    panel = app.query_one("#panel-3", Panel)
    rows = app.query_one("#proc-table", DataTable).row_count
    filtered = panel.filter_text == "python" and rows > 0
    if not filtered:
        failures += 1
    print(
        f"  filter typing: filter={panel.filter_text!r} rows={rows} "
        f"{'OK' if filtered else 'FAILED'}"
    )
    print(
        f"  Enter returns focus to the table: "
        f"{'OK' if app.focused and app.focused.id == 'proc-table' else 'FAILED'}"
    )
    # Escape must clear the way out of a focused input.
    await pilot.press("slash")
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()
    print(
        f"  Escape leaves the filter: "
        f"{'OK' if not isinstance(app.focused, Input) else 'FAILED'}"
    )
    panel.apply_filter("")
    return failures


async def main() -> int:
    print("CLI")
    failures = check_cli()

    app = SysmonApp()
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        # Let the first sample of the launch panel land.
        await asyncio.sleep(2.0)
        await pilot.pause()

        failures += await check_overview(pilot)
        failures += await check_switching(pilot)
        await asyncio.sleep(1.0)

        for index, _panel_class in enumerate(PANELS):
            if index != 0:
                app.show_panel(index)
                await pilot.pause()
            await asyncio.sleep(2.2)
            await pilot.pause()
            panel = app.current
            dump_panel(panel)
            if panel._error:
                failures += 1
                print(f"  !!! panel reported an error: {panel._error}")

        # Exercise the interactive bindings.
        print(f"\n{'=' * 100}\nKEY BINDINGS\n{'=' * 100}")
        app.show_panel(3)  # Processes
        await pilot.pause()
        proc_panel = app.current
        for label, action in (
            ("cycle_sort", app.action_cycle_sort),
            ("reverse_sort", app.action_reverse_sort),
            ("show_sort", app.action_show_sort),
        ):
            try:
                action()
                print(
                    f"  {label:<14} OK  sort_key={proc_panel.sort_key} reverse={proc_panel.reverse}"
                )
            except Exception as exc:
                failures += 1
                print(f"  {label:<14} FAILED: {type(exc).__name__}: {exc}")

        try:
            proc_panel.apply_filter("python")
            await asyncio.sleep(1.5)
            await pilot.pause()
            table = proc_panel.query_one("#proc-table", DataTable)
            print(f"  filter 'python'  OK  {table.row_count} rows matched")
        except Exception as exc:
            failures += 1
            print(f"  filter           FAILED: {type(exc).__name__}: {exc}")
        proc_panel.apply_filter("")

        app.show_panel(4)  # GPU
        await pilot.pause()
        await asyncio.sleep(1.5)
        await pilot.pause()
        try:
            app.action_toggle_gpu()
            print(f"  toggle_gpu       OK  compute_only={app.current.compute_only}")
            app.action_toggle_gpu()
        except Exception as exc:
            failures += 1
            print(f"  toggle_gpu       FAILED: {type(exc).__name__}: {exc}")

        for label, action in (
            ("prev_panel", app.action_prev_panel),
            ("next_panel", app.action_next_panel),
            ("refresh", app.action_refresh),
            ("focus_filter", app.action_focus_filter),
        ):
            try:
                action()
                await pilot.pause()
                print(f"  {label:<14} OK")
            except Exception as exc:
                failures += 1
                print(f"  {label:<14} FAILED: {type(exc).__name__}: {exc}")

        await asyncio.sleep(1.0)
        await pilot.pause()

    print(f"\n{'=' * 100}")
    print(
        "RESULT:",
        "all panels rendered without error"
        if not failures
        else f"{failures} failure(s)",
    )
    print("=" * 100)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
