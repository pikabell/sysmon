"""Layout regression checks.

These assert things about the *rendered* UI that unit tests on the data sources
cannot catch: that content fits inside the terminal, that nothing is truncated,
and that the tab bar actually shows every panel.

Textual has no public "give me the text of this widget" API, so these render each
panel into a fixed-width Rich console and inspect the resulting lines.

Run with:  uv run python tests/test_layout.py
"""

from __future__ import annotations

import io
import sys

from rich.console import Console
from rich.text import Text

from sysmon.app import SysmonApp
from sysmon.core.registry import PANELS
from sysmon.panels.cpu import CPUPanel
from sysmon.panels.gpu import GPUPanel
from sysmon.panels.memory import MemoryPanel
from sysmon.sources.gpu import GPUSnapshot

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def render(renderable, width: int) -> list[str]:
    console = Console(
        width=width, no_color=True, legacy_windows=False, file=io.StringIO()
    )
    console.print(renderable)
    return console.file.getvalue().splitlines()


def overflowing(renderable, width: int) -> list[str]:
    return [line for line in render(renderable, width) if len(line.rstrip()) > width]


def truncated(renderable, width: int) -> list[str]:
    """Lines running to the console's exact right edge.

    Rich clips with an ellipsis, which shows up as the line reaching column
    ``width`` exactly. That is how the memory stack bar was caught overflowing.
    """
    return [line for line in render(renderable, width) if len(line.rstrip()) == width]


def test_memory_panel() -> None:
    print("\nmemory panel")
    panel = MemoryPanel()
    stats = panel.fetch()
    for width in (80, 100, 150):
        view = panel._view(stats, width=width - 4)
        check(
            f"memory fits at {width} columns",
            not overflowing(view, width),
            f"{len(overflowing(view, width))} overlong lines",
        )
    view = panel._view(stats)
    clipped = truncated(view, 100)
    check("memory bar is not truncated", not clipped, f"{len(clipped)} clipped line(s)")
    body = "\n".join(render(view, 100))
    check(
        "memory legend shows every segment",
        "used" in body and "cache" in body and "free" in body,
    )


def test_cpu_panel() -> None:
    print("\ncpu panel")
    panel = CPUPanel()
    panel.history = [0.2, 0.9, 1.0, 0.5, 0.7]
    stats = panel.fetch()
    for width in (80, 120, 150):
        lines = render(panel._header(stats), width)
        check(
            f"cpu header fits at {width} columns",
            all(len(line) <= width for line in lines),
            f"widest {max(len(line) for line in lines)}",
        )
    aggregate = panel._aggregate(stats)
    for width in (80, 120, 150):
        check(
            f"cpu summary fits at {width} columns",
            not overflowing(aggregate, width),
        )
    cores = panel._cores(stats)
    for width in (80, 120, 150):
        check(
            f"per-core grid fits at {width} columns",
            not overflowing(cores, width),
        )


def test_gpu_cards() -> None:
    print("\ngpu cards")
    panel = GPUPanel()
    snapshot = panel.fetch()
    if not snapshot.devices:
        print("  [SKIP] no NVIDIA GPU on this machine")
        return
    for width in (80, 100, 120, 150, 200):
        view = panel._devices(snapshot)
        lines = render(view, width)
        over = [line for line in lines if len(line.rstrip()) > width]
        check(
            f"gpu cards fit at {width} columns",
            not over,
            f"{len(over)} overlong line(s)",
        )
    # Every GPU must be named somewhere on the card block.
    view = panel._devices(snapshot)
    body = "\n".join(render(view, 200))
    missing = [
        device.index for device in snapshot.devices if f"GPU {device.index}" not in body
    ]
    check(
        "every GPU appears in the card block",
        not missing,
        f"missing {missing}" if missing else f"{len(snapshot.devices)} cards",
    )


def test_gpu_process_rows() -> None:
    print("\ngpu process table")
    panel = GPUPanel()
    snapshot = panel.fetch()
    if not snapshot.processes:
        print("  [SKIP] no GPU processes")
        return
    rows = panel._rows(snapshot)
    # A process spanning several GPUs must produce one row per GPU.
    expected = len(panel._visible(snapshot))
    check(
        "one row per (pid, gpu) pair",
        len(rows) == expected,
        f"{len(rows)} rows for {expected} processes",
    )
    keys = list(rows)
    check("row keys are unique", len(set(keys)) == len(keys), f"{len(keys)} keys")
    order = panel._order(snapshot)
    check(
        "order contains no duplicates",
        len(order) == len(set(order)),
        f"{len(order)} entries",
    )
    check(
        "order matches row keys",
        set(order) == set(keys),
        "ordering disagrees with rows",
    )
    widths = {len(cells) for cells in rows.values()}
    check("all rows have the same width", len(widths) == 1, f"widths {widths}")

    # A process on several GPUs (Xorg runs on all four) must get one row per GPU
    # rather than being collapsed into a single row.
    per_pid: dict[int, set[int]] = {}
    for pid, gpu in keys:
        per_pid.setdefault(pid, set()).add(gpu)
    multi = {pid for pid, gpus in per_pid.items() if len(gpus) > 1}
    ok = all(
        len([key for key in keys if key[0] == pid]) == len(per_pid[pid])
        for pid in multi
    )
    check(
        "multi-GPU processes get one row per GPU",
        ok,
        f"{len(multi)} multi-GPU process(es)",
    )


def test_tab_bar() -> None:
    """The tab bar must render every panel within the terminal width."""
    print("\ntab bar")
    labels = [panel.KEY for panel in PANELS]
    for width in (80, 120):
        joined = "".join(SysmonApp.tab_label(panel, width) + "  " for panel in PANELS)
        check(
            f"tab labels fit at {width} columns",
            len(joined) <= width,
            f"{len(joined)} chars",
        )
    check(
        "every panel has a tab",
        len(labels) == len(PANELS),
        f"{len(labels)} tabs for {len(PANELS)} panels",
    )
    # Tab keys must be unique and single digits, since they are pressed directly.
    keys = [panel.KEY for panel in PANELS]
    check("tab keys are unique", len(set(keys)) == len(keys), str(keys))
    check("tab keys are single digits", all(k.isdigit() and len(k) == 1 for k in keys))


def test_panel_class_names() -> None:
    """Textual's Static has a private ``_render`` hook.

    Overriding it in a panel breaks rendering, and the failure is a confusing
    TypeError deep inside the compositor rather than anything in this codebase.
    """
    print("\nwidget names")
    for panel_class in PANELS:
        bad = [
            name
            for name in vars(panel_class)
            if name in ("_render", "render_lines", "render_line")
        ]
        check(
            f"{panel_class.__name__} avoids Textual internals",
            not bad,
            f"overrides {bad}" if bad else "",
        )


def test_source_sharing() -> None:
    """Sources must be shared, or switching tabs re-runs expensive work."""
    print("\nsource sharing")
    from sysmon.core.context import SOURCES

    check("process source is a singleton", SOURCES.proc is SOURCES.proc)
    check("cpu source is a singleton", SOURCES.cpu is SOURCES.cpu)
    check("memory source is a singleton", SOURCES.mem is SOURCES.mem)
    # net and gpu need the process source; building them must not deadlock on
    # the lock, which a plain Lock would when resolving proc re-entrantly.
    check(
        "net resolves through the shared process source",
        SOURCES.net._procs is SOURCES.proc,
    )
    check(
        "gpu resolves through the shared process source",
        SOURCES.gpu._procs is SOURCES.proc,
    )

    # Warm must be idempotent and must not raise.
    SOURCES.warm()
    check("warm is idempotent", SOURCES.warmed, "second call should be a no-op")


def test_bars() -> None:
    """Bars must have sub-cell resolution and never exceed their width."""
    print("\nbars")
    from sysmon.core.render import BAR_EMPTY, bar_cells, meter

    for width in (6, 10, 20, 30):
        for fraction in (0.0, 0.01, 0.25, 0.37, 0.5, 0.66, 0.99, 1.0, None):
            text = bar_cells(fraction, width)
            plain = text.plain
            ok = len(plain) == width
            check(
                f"bar width exact at {width} cells for {fraction}",
                ok,
                f"got {len(plain)}: {plain!r}",
            )

    # Sub-cell resolution: 37% of 10 cells must not round to the same bar as 50%.
    check(
        "bar distinguishes 37% from 50% on 10 cells",
        bar_cells(0.37, 10).plain != bar_cells(0.5, 10).plain,
        f"{bar_cells(0.37, 10).plain!r} vs {bar_cells(0.5, 10).plain!r}",
    )
    check(
        "bar distinguishes 0% from 1%",
        bar_cells(0.0, 10).plain != bar_cells(0.01, 10).plain,
    )
    # Monotonic: more utilisation never yields fewer filled cells.
    previous = 0
    monotonic = True
    for step in range(0, 101):
        filled = sum(1 for ch in bar_cells(step / 100, 20).plain if ch != BAR_EMPTY)
        if filled < previous:
            monotonic = False
        previous = filled
    check("bar fill is monotonic in the value", monotonic)

    # A one-cell micro-bar still distinguishes 0% from 100% by glyph.
    check(
        "micro-bar glyph mode still shows fill",
        bar_cells(1.0, 6, glyph="▰").plain == "▰▰▰▰▰▰"
        and bar_cells(0.0, 6, glyph="▰").plain == BAR_EMPTY * 6,
    )
    check(
        "unreadable value is distinct from idle",
        "?" in bar_cells(None, 8).plain
        and bar_cells(None, 8).plain != bar_cells(0, 8).plain,
    )
    check("zero-width bar is safe", bar_cells(0.5, 0).plain == "")
    # The meter helper composes label, bar and value without overflowing.
    lines = render(meter("total", 0.5, Text("50%"), 20), 40)
    check("meter fits its budget", all(len(line) <= 40 for line in lines))


def test_overview_layout() -> None:
    """The overview must not let any section be squeezed below legibility."""
    print("\noverview layout")
    from sysmon.panels.overview import MEDIUM, WIDE, OverviewPanel

    panel = OverviewPanel()
    snapshot = panel.fetch()
    for width in (80, 100, 112, 120, 146, 180, 220):
        columns = panel._columns(width)
        share = (width - 2 * (columns - 1) - 2) // columns
        view = panel._view(snapshot.summary, width=width - 4)
        lines = [line for line in render(view, width) if line.strip()]
        over = [line for line in lines if len(line.rstrip()) > width]
        check(
            f"overview fits at {width} columns",
            not over,
            f"{len(over)} overlong line(s)",
        )
        # Each column must get enough room for a pid, a value and a command.
        check(
            f"column share usable at {width} columns",
            share >= 40,
            f"{columns} column(s), share {share}",
        )
    check("breakpoints are ordered", MEDIUM < WIDE, f"{MEDIUM} < {WIDE}")
    check("wide layout needs three columns", panel._columns(WIDE) == 3)
    check("narrow layout is single column", panel._columns(MEDIUM - 1) == 1)


def test_overview_panel() -> None:
    """The landing page must fit and show the essentials."""
    print("\noverview")
    from sysmon.panels.overview import OverviewPanel

    panel = OverviewPanel()
    snapshot = panel.fetch()
    summary = snapshot.summary
    view = panel._view(summary)
    for width in (80, 100, 120, 150, 200):
        lines = render(view, width)
        over = [line for line in lines if len(line.rstrip()) > width]
        check(
            f"overview fits at {width} columns",
            not over,
            f"{len(over)} overlong line(s)",
        )
    body = "\n".join(render(view, 150))
    # Every essential section must be present.
    for section in ("cpu", "memory", "gpu", "network", "top cpu", "top memory"):
        check(f"overview shows {section!r}", section in body.lower())
    # And it must actually be summarising, not dumping the whole table.
    check(
        "overview lists a handful of processes, not all",
        len(summary.top_cpu) <= 5 and len(summary.top_memory) <= 5,
        f"{len(summary.top_cpu)} cpu / {len(summary.top_memory)} memory",
    )
    check(
        "top processes are actually busy",
        all(item.info.cpu_pct >= 1.0 for item in summary.top_cpu),
        "threshold filter works",
    )
    check(
        "memory ranking is descending",
        all(
            summary.top_memory[i].info.rss_bytes
            >= summary.top_memory[i + 1].info.rss_bytes
            for i in range(len(summary.top_memory) - 1)
        ),
    )


def test_alerts() -> None:
    """Alerts must be quiet when healthy and loud when not."""
    print("\nalerts")
    from sysmon.sources.cpu import CPUStats
    from sysmon.sources.memory import MemStats
    from sysmon.sources.net import NetSnapshot
    from sysmon.sources.nvml import GPUDevice
    from sysmon.sources.summary import build_alerts

    def cpu_stats(total: float, iowait: float = 0.0, cores: int = 16) -> CPUStats:
        return CPUStats(
            total=total,
            user=total / 2,
            system=total / 2,
            iowait=iowait,
            steal=0.0,
            cores=(),
            load1=1.0,
            load5=1.0,
            load15=1.0,
            freq_mhz=3000,
            model="test",
            online_cores=cores,
        )

    def gpu_snapshot(devices=()) -> GPUSnapshot:
        return GPUSnapshot(
            devices=tuple(devices),
            processes=(),
            details={},
            source="nvml",
            driver="test",
            error=None,
            collected_at=0.0,
        )

    healthy_mem = MemStats(
        total=64 << 30,
        available=32 << 30,
        free=16 << 30,
        cached=8 << 30,
        buffers=0,
        slab=0,
        used=20 << 30,
        swap_total=8 << 30,
        swap_used=0,
        dirty=0,
    )
    empty_net = NetSnapshot(ifaces=(), conns=(), degraded=False, error=None)

    idle = build_alerts(cpu_stats(0.2), healthy_mem, gpu_snapshot(), empty_net)
    check("no alerts on a healthy machine", not idle, f"{idle}")

    def messages(alerts) -> list[str]:
        return [alert.message for alert in alerts]

    def join(alerts) -> str:
        return " ".join(messages(alerts))

    # Severity must ride along with the message so the panel can style it
    # without re-deriving which alerts matter.
    hot_alerts = build_alerts(
        cpu_stats(0.5),
        healthy_mem,
        gpu_snapshot(
            (
                GPUDevice(
                    index=0,
                    name="test",
                    util_gpu=99,
                    util_mem=50,
                    mem_used=16 << 30,
                    mem_total=16 << 30,
                    temperature=91,
                    power_w=200,
                    power_limit_w=230,
                    fan=90,
                    clock_mhz=1000,
                    mem_clock_mhz=1000,
                ),
            )
        ),
        empty_net,
    )
    check(
        "every alert carries a severity",
        all(1 <= alert.severity <= 3 for alert in hot_alerts),
        str([a.severity for a in hot_alerts]),
    )
    check(
        "alerts are ordered most severe first",
        [a.severity for a in hot_alerts] == sorted(a.severity for a in hot_alerts),
        str([a.severity for a in hot_alerts]),
    )

    hot = build_alerts(
        cpu_stats(0.5),
        healthy_mem,
        gpu_snapshot(
            (
                GPUDevice(
                    index=0,
                    name="test",
                    util_gpu=99,
                    util_mem=50,
                    mem_used=16 << 30,
                    mem_total=16 << 30,
                    temperature=91,
                    power_w=200,
                    power_limit_w=230,
                    fan=90,
                    clock_mhz=1000,
                    mem_clock_mhz=1000,
                ),
            )
        ),
        empty_net,
    )
    joined = join(hot)
    check("hot GPU is flagged", "gpu 0 hot" in joined, joined)
    check("busy GPU alone is not flagged", "100% util" not in joined, joined)

    # Several saturated GPUs must collapse into one line, not N lines.
    multi = build_alerts(
        cpu_stats(0.5),
        healthy_mem,
        gpu_snapshot(
            [
                GPUDevice(
                    index=i,
                    name="test",
                    util_gpu=100,
                    util_mem=50,
                    mem_used=1 << 30,
                    mem_total=16 << 30,
                    temperature=60,
                    power_w=100,
                    power_limit_w=230,
                    fan=40,
                    clock_mhz=1000,
                    mem_clock_mhz=1000,
                )
                for i in range(4)
            ]
        ),
        empty_net,
    )
    saturation_lines = [a for a in messages(multi) if "100% util" in a]
    check(
        "saturated GPUs doing work are not health alerts",
        len(saturation_lines) == 0,
        str(saturation_lines),
    )

    # 78 C is worth mentioning but must not be called "hot".
    warm = build_alerts(
        cpu_stats(0.2),
        healthy_mem,
        gpu_snapshot(
            (
                GPUDevice(
                    index=0,
                    name="test",
                    util_gpu=0,
                    util_mem=0,
                    mem_used=0,
                    mem_total=16 << 30,
                    temperature=78,
                    power_w=10,
                    power_limit_w=230,
                    fan=30,
                    clock_mhz=300,
                    mem_clock_mhz=405,
                ),
            )
        ),
        empty_net,
    )
    check(
        "78C is warm, not hot",
        any("warm: 78" in a for a in messages(warm))
        and not any("hot:" in a for a in messages(warm)),
        str(messages(warm)),
    )

    starved = build_alerts(
        cpu_stats(0.3),
        MemStats(
            total=64 << 30,
            available=1 << 30,
            free=0,
            cached=0,
            buffers=0,
            slab=0,
            used=63 << 30,
            swap_total=8 << 30,
            swap_used=6 << 30,
            dirty=0,
        ),
        gpu_snapshot(),
        empty_net,
    )
    check(
        "low memory is flagged",
        any("memory low" in a for a in messages(starved)),
        str(messages(starved)),
    )
    check(
        "heavy swap is flagged",
        any("swap in use" in a for a in messages(starved)),
        str(messages(starved)),
    )

    # CPU saturation needs load above the core count as well as high busy time;
    # load1 is 1.0 here, which is nowhere near 16 cores' worth of work.
    saturated = build_alerts(
        cpu_stats(0.98, cores=16), healthy_mem, gpu_snapshot(), empty_net
    )
    check(
        "high busy alone is not an alert",
        not any("cpu saturated" in a for a in messages(saturated)),
        "needs load above core count too",
    )

    def loaded(total: float, load1: float, cores: int = 16) -> CPUStats:
        stats = cpu_stats(total, cores=cores)
        return CPUStats(
            total=stats.total,
            user=stats.user,
            system=stats.system,
            iowait=0.0,
            steal=0.0,
            cores=(),
            load1=load1,
            load5=load1,
            load15=load1,
            freq_mhz=3000,
            model="test",
            online_cores=cores,
        )

    really_saturated = build_alerts(
        loaded(0.98, 22.0), healthy_mem, gpu_snapshot(), empty_net
    )
    check(
        "saturated CPU with high load is flagged",
        any("cpu saturated" in a for a in messages(really_saturated)),
        str(messages(really_saturated)),
    )
    io_bound = build_alerts(
        cpu_stats(0.4, iowait=0.4), healthy_mem, gpu_snapshot(), empty_net
    )
    check(
        "high iowait is flagged",
        any("iowait" in a for a in messages(io_bound)),
        str(messages(io_bound)),
    )


async def test_rendered_panels() -> None:
    """Boot the app and confirm every panel renders and fits."""
    print("\nrendered panels")
    app = SysmonApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        import asyncio

        await asyncio.sleep(2.0)
        for index, panel_class in enumerate(PANELS):
            if index:
                app.show_panel(index)
                await pilot.pause()
            await asyncio.sleep(2.2)
            panel = app.current
            check(
                f"panel {index + 1} ({panel_class.TITLE}) renders without error",
                panel._error is None,
                panel._error or "",
            )


def main() -> int:
    test_source_sharing()
    test_bars()
    test_overview_layout()
    test_overview_panel()
    test_alerts()
    test_memory_panel()
    test_cpu_panel()
    test_gpu_cards()
    test_gpu_process_rows()
    test_tab_bar()
    test_panel_class_names()

    import asyncio

    asyncio.run(test_rendered_panels())

    print(f"\n{'=' * 70}")
    if failures:
        print(f"RESULT: {len(failures)} failure(s): {', '.join(failures)}")
    else:
        print("RESULT: all layout checks passed")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
