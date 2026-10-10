"""Shared Rich rendering helpers used by the panels."""

from __future__ import annotations

import datetime as _dt
import time

from rich.table import Table
from rich.text import Text

# Eight blocks, indexed by eighths of a cell. This gives a bar sub-cell
# resolution, so 37% of a 10-cell bar draws as 3.7 cells rather than rounding to
# either 3 or 4 - which on a narrow bar reads as the difference between "mostly
# idle" and "busy".
_BLOCKS = " ▏▎▍▌▋▊▉▰"

# A single-cell glyph, for micro-bars in dense tables.
BAR_FULL = "▰"
BAR_EMPTY = "·"

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")

# Default track colour: dark enough to recede, light enough to show the scale.
TRACK = "grey23"


def heat(fraction: float, *, warn: float = 0.55, crit: float = 0.85) -> str:
    """Pick a colour style for a 0..1 utilisation value."""
    if fraction is None:
        return "grey50"
    if fraction >= crit:
        return "bold red"
    if fraction >= warn:
        return "yellow"
    return "green"


def _segments(fraction: float, width: int) -> tuple[int, int]:
    """Return ``(whole_cells, eighths_of_next_cell)`` for a bar of ``width``."""
    eighths = int(round(max(0.0, min(1.0, fraction)) * width * 8))
    whole, remainder = divmod(eighths, 8)
    return min(whole, width), (0 if whole >= width else remainder)


def bar_cells(
    fraction: float | None,
    width: int,
    *,
    warn: float = 0.55,
    crit: float = 0.85,
    style: str | None = None,
    track: str = TRACK,
    glyph: str | None = None,
    border: bool = True,
) -> Text:
    """A utilisation bar with sub-cell resolution and a visible boundary.

    Three things stop this reading as a flat block of colour:

    * **Sub-cell fill.** The leading edge lands on an eighth of a cell, so short
      bars still show partial utilisation.
    * **A dimmer leading edge.** The partial block and the last solid cell are
      dimmed, which draws the eye to the boundary of the fill.
    * **A track that recedes.** Unfilled cells are dark, so the bar looks like a
      gauge with a scale rather than a solid rectangle.

    Set ``glyph`` for a one-cell micro-bar (dense tables), or ``border=False`` for
    a plain two-tone bar.
    """
    if width <= 0:
        return Text("")
    if fraction is None:
        return Text("?".center(width, "─"), style="grey50")

    fraction = max(0.0, min(1.0, fraction))
    whole, remainder = _segments(fraction, width)
    fill = style or heat(fraction, warn=warn, crit=crit)

    text = Text(no_wrap=True)
    if glyph:
        # Track uses the empty glyph, not the fill glyph: a micro-bar has no room
        # for a partial block, and identical glyphs would make 0% and 100% look
        # the same in a terminal without colour.
        text.append(glyph * whole, style=fill)
        text.append(BAR_EMPTY * (width - whole), style=track)
        return text

    # Solid run: dim the final cell so the fill boundary is locatable at a glance.
    if whole:
        solid = min(whole, width - 1) if (border and remainder) else whole
        if solid:
            text.append(_BLOCKS[8] * solid, style=fill)
        if whole > solid:
            text.append(_BLOCKS[8], style=fill + " dim")

    if remainder:
        text.append(_BLOCKS[remainder], style=fill + " dim")

    used = whole + (1 if remainder else 0)
    if used < width:
        text.append(BAR_EMPTY * (width - used), style=track)
    return text


def bar(fraction: float | None, width: int = 24, **kwargs) -> Text:
    """A horizontal utilisation bar. See :func:`bar_cells`."""
    return bar_cells(fraction, width, **kwargs)


def sparkline(values: list[float], width: int) -> Text:
    """Recent samples on a fixed 0–100% scale; newest at the right."""
    levels = "▁▂▃▄▅▆▇█"
    shown = values[-max(1, width) :]
    text = Text(" " * max(0, width - len(shown)), no_wrap=True)
    for value in shown:
        value = max(0.0, min(1.0, value))
        text.append(levels[min(7, int(value * 8))], style=heat(value))
    return text


def meter(
    label: str,
    fraction: float | None,
    value,
    width: int,
    *,
    label_width: int = 5,
    **kwargs,
) -> Table:
    """One ``label [bar] value`` line, as a borderless grid."""
    row = Table.grid(padding=(0, 1))
    row.add_column(no_wrap=True, width=label_width, justify="right")
    row.add_column(no_wrap=True, width=width)
    row.add_column(no_wrap=True, justify="left")
    row.add_row(
        Text(label, style="grey58"),
        bar_cells(fraction, width, **kwargs),
        value,
    )
    return row


def pct(fraction: float | None, digits: int = 0, width: int = 4) -> Text:
    """A right-aligned percentage, or ``-`` when the driver cannot report it."""
    if fraction is None:
        return Text("-".rjust(max(1, width - 1)), style="grey42")
    return Text(
        f"{fraction * 100:>{max(1, width - 1)}.{digits}f}%",
        style=heat(fraction),
    )


def human_bytes(value: float | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in _BYTE_UNITS:
        if abs(size) < 1024.0 or unit == _BYTE_UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} {_BYTE_UNITS[-1]}"


def human_rate(bytes_per_second: float | None) -> str:
    if bytes_per_second is None:
        return "-"
    return human_bytes(bytes_per_second) + "/s"


def human_count(value: float | None) -> str:
    if value is None:
        return "-"
    for unit in ("", "K", "M", "G", "T"):
        if abs(value) < 1000.0 or unit == "T":
            return f"{value:.0f}" if not unit else f"{value:.1f}{unit}"
        value /= 1000.0
    return f"{value:.1f}T"


def human_duration(seconds: float | None) -> str:
    """Compact age, e.g. ``3d04h``, ``12m03s``, ``1.42s``."""
    if seconds is None:
        return "-"
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def fmt_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "unavailable"
    days, remainder = divmod(max(0, int(seconds)), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{days}d " if days else "") + f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def fmt_start(timestamp: float | None) -> str:
    """Absolute wall-clock start time, date-aware for old processes."""
    if timestamp is None:
        return "-"
    stamp = _dt.datetime.fromtimestamp(timestamp)
    now = _dt.datetime.now()
    same_year = stamp.year == now.year
    return stamp.strftime("%b %d %H:%M" if same_year else "%b %d  %Y")


def fmt_clock(timestamp: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(
        time.time() if timestamp is None else timestamp
    ).strftime("%H:%M:%S")


def fmt_timestamp(timestamp: float | None) -> str:
    """Full local process creation time including seconds."""
    if timestamp is None:
        return "-"
    return _dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def kv_grid(pairs: list[tuple[str, object]], *, label_width: int = 10) -> Table:
    """A borderless two-column label/value grid."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="grey58", no_wrap=True, width=label_width, justify="right")
    grid.add_column(justify="left")
    for label, value in pairs:
        grid.add_row(label, value if isinstance(value, Text) else Text(str(value)))
    return grid
