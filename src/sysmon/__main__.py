from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .app import HELP_TEXT, SysmonApp
from .core.registry import PANELS
from .config import config_path, load, validate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysmon",
        description="A general-purpose terminal system monitor.",
        epilog="Run without arguments to launch the full-screen interface.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sysmon {__version__}")
    parser.add_argument(
        "--panel",
        choices=[panel.KEY for panel in PANELS],
        metavar="N",
        help="open directly on a panel (default: 0, Overview)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print Linux capability checks without opening the UI",
    )
    parser.add_argument(
        "--help-keys",
        action="store_true",
        help="print the in-app key bindings and exit",
    )
    parser.add_argument(
        "--config", type=Path, help="settings JSON path (default: XDG config directory)"
    )
    parser.add_argument(
        "--refresh", type=float, help="override refresh interval, 0.2–60 seconds"
    )
    parser.add_argument(
        "--export",
        type=Path,
        metavar="PATH",
        help="write a fresh private JSON snapshot and exit",
    )
    parser.add_argument(
        "--include-commands",
        action="store_true",
        help="include commands in JSON exports",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.help_keys:
        from rich.console import Console

        Console().print(HELP_TEXT, highlight=False)
        return 0

    if args.check:
        from .diagnostics import check

        report, code = check()
        print(report)
        return code

    if not sys.platform.startswith("linux"):
        print("sysmon requires Linux with procfs mounted at /proc.", file=sys.stderr)
        return 2

    try:
        path = args.config if args.config is not None else config_path()
        settings = load(path)
        if args.refresh is not None:
            raw = asdict(settings)
            raw["refresh_interval"] = args.refresh
            settings = validate(raw)
        if args.export is not None:
            import time
            from .core.context import SOURCES
            from .export import snapshot, write_report

            SOURCES.warm()
            time.sleep(0.2)
            write_report(
                snapshot(include_commands=args.include_commands),
                args.export.expanduser(),
            )
            print(f"Saved {args.export}")
            return 0
        app = SysmonApp(settings, path)
    except (OSError, ValueError) as exc:
        print(f"sysmon: {exc}", file=sys.stderr)
        return 2
    if args.panel:
        app.open_panel_on_start([panel.KEY for panel in PANELS].index(args.panel))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
