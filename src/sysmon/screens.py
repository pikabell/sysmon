"""Read-only inspection, history, settings, and explicit export dialogs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from .config import Settings, save, validate
from .core.context import SOURCES
from .core.render import fmt_timestamp, human_bytes, human_duration
from .export import snapshot, write_report
from .history import plot
from .sources.details import inspect_process

METRIC_HELP = {
    "0": "Overview shows host CPU/RAM separately from this monitor's cgroup.\nCPU busy excludes idle and iowait; 100% spans all visible host CPUs.\nRAM used = total − available; reclaimable cache is available, not wasted.\nPressure is time stalled, not utilisation. Disk device rates are not summed\nacross stacked devices. Missing counters stay unavailable. H expands history.",
    "1": "CPU busy includes user/nice, system/interrupts and steal, excludes idle\nand iowait. Guest ticks are already included. Load counts runnable or\nuninterruptible tasks; compare with logical CPU count. Iowait is approximate\naccounting, not disk throughput. These are host counters; cgroup limits are on 7.",
    "2": "RAM used = total − available. Available includes reclaimable cache.\nCache, buffers, slab and dirty pages overlap; do not add them together.\nSwap occupation does not indicate current swapping. Older kernels use an\navailability estimate. These are host counters; cgroup usage/limits are on 7.",
    "3": "Process CPU: 100% = one logical CPU, so threaded jobs may exceed 100%.\nRSS can count shared pages in more than one process. Start = kernel boot time\nplus process start ticks. Enter or d inspects the selected process. Open files\nand I/O often require matching ownership; hidepid may hide other processes.\nInspection never opens file targets or changes process state.",
    "4": "STARTED is the process creation time in your local timezone, including\nseconds. It is not the time a CUDA kernel first ran. SM% and MEM IO% are\nactivity; VRAM is allocated bytes. Native NVML usually supplies VRAM, while\npmon may supply activity. Missing values are unknown, not zero. Enter or d\ninspects the selected job. GPU thermal notices use configurable policy limits.",
    "5": "RX/TX rates are byte-counter deltas over monotonic time. Counter resets\nestablish a new baseline. Errors in the interface table are lifetime totals;\noverview notices count new errors only. Socket visibility/owners follow kernel\npermissions. Hidden interfaces affect display; sockets are not interface-mapped.",
    "6": "Disk sectors in /proc/diskstats are 512 bytes. Rates use counter deltas;\nnew devices/resets need a baseline. BUSY% is elapsed device I/O time, not an\nSSD saturation score. Devices, partitions, RAID and dm layers overlap: do not\nsum them blindly. Filesystem USED% = used / (used + user-available), excluding\nreserved blocks. Inodes can run out before bytes. Capacity is cached for 10s.\nRemote/FUSE capacity queries are skipped to avoid blocked mounts hanging reads.",
    "7": "PSI some = at least one task stalled; full = all non-idle tasks stalled.\nThe percentages cover 10/60/300 seconds. CPU full is undefined system-wide.\nCgroup CPU% = CPU seconds / elapsed / effective visible CPU capacity × 100.\nCapacity considers affinity/cpuset and visible ancestor quotas. Memory capacity\nis the smallest visible hard limit capped by host RAM. Usage includes this\ncgroup and descendants; it can include other jobs. Hidden ancestors and other\ncontainers are outside this view. Permission errors remain explicit.",
}


class ViewerScreen(ModalScreen):
    BINDINGS = [Binding("escape,q", "close", show=False)]

    def __init__(self, title: str, content: str):
        super().__init__()
        self.title_text, self.content_text = title, content

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(Text(self.title_text, style="bold"), classes="dialog-title")
            yield Static(Text(self.content_text), id="viewer-content")

    def on_mount(self):
        self.query_one(VerticalScroll).focus()

    def action_close(self):
        self.dismiss()


class ProcessScreen(ViewerScreen):
    def __init__(self, info):
        super().__init__(
            f"Process {info.pid} · read-only snapshot · Esc closes", "Reading details…"
        )
        self.info = info

    def on_mount(self):
        super().on_mount()
        self.run_worker(self._load(), exclusive=True)

    async def _load(self):
        try:
            details = await asyncio.to_thread(
                SOURCES.collect, lambda: inspect_process(SOURCES.proc, self.info)
            )
            info = details.info
            lines = [
                f"PID {info.pid}  parent {info.ppid}  user {info.username}",
                f"Started {fmt_timestamp(info.start_ts)} (local) · age {human_duration(info.age)}",
                f"State {info.state_label} · RSS {human_bytes(info.rss_bytes)} · threads {info.threads}",
                f"Command: {info.cmdline}",
                "",
                "Status:",
            ]
            lines.extend(f"{key}: {value}" for key, value in details.status)
            lines.extend(["", "Cumulative process I/O (bytes or syscalls; not rates):"])
            lines.extend(f"{key}: {value}" for key, value in details.io)
            lines.extend(
                [
                    "",
                    "Resource limits:",
                    details.limits or "unavailable",
                    "Open files (targets are not opened):",
                ]
            )
            lines.extend(f"fd {fd}: {target}" for fd, target in details.files)
            lines.extend(["", "Threads: TID · name · state"])
            lines.extend(
                f"{tid} · {name} · {state}" for tid, name, state in details.threads
            )
            lines.extend(["", *details.notes])
            self.query_one("#viewer-content", Static).update(Text("\n".join(lines)))
        except (OSError, ValueError) as exc:
            self.query_one("#viewer-content", Static).update(
                Text(f"Details unavailable: {exc}", style="yellow")
            )


class HistoryScreen(ModalScreen):
    BINDINGS = [Binding("escape,q", "close", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static(
                "Session history · only sampled panels contribute · Esc closes",
                classes="dialog-title",
            )
            names = sorted(self.app.history.series)
            yield Select(
                [(name, name) for name in names],
                value=names[0] if names else Select.BLANK,
                id="history-metric",
            )
            yield Static(id="history-plot")

    def on_mount(self):
        self._redraw()
        self.set_interval(1, self._redraw)

    def on_select_changed(self, event: Select.Changed):
        self._redraw()

    def _redraw(self):
        select = self.query_one("#history-metric", Select)
        name = select.value
        if name is Select.BLANK:
            rendered = Text(
                "No history yet. Close this view and collect a sample first."
            )
        else:
            rendered = plot(
                list(self.app.history.series.get(name, ())),
                self.app.history.units.get(name, "%"),
                max(10, min(100, self.size.width - 22)),
            )
        self.query_one("#history-plot", Static).update(rendered)

    def action_close(self):
        self.dismiss()


class SettingsScreen(ModalScreen[Settings | None]):
    BINDINGS = [Binding("escape", "close", show=False, priority=True)]

    def compose(self) -> ComposeResult:
        settings = self.app.settings
        with VerticalScroll(classes="dialog"):
            yield Static(
                "Settings · save applies now; default panel applies next launch",
                classes="dialog-title",
            )
            for name, label, value in (
                (
                    "refresh_interval",
                    "Refresh interval (0.2–60 seconds)",
                    settings.refresh_interval,
                ),
                ("default_panel", "Default panel (0–7)", settings.default_panel),
                (
                    "hidden_interfaces",
                    "Hidden interfaces (comma separated)",
                    ",".join(settings.hidden_interfaces),
                ),
                (
                    "history_samples",
                    "Samples retained per metric (10–3600)",
                    settings.history_samples,
                ),
            ):
                yield Label(label)
                yield Input(str(value), id=f"setting-{name}")
            yield Label("Theme")
            yield Select(
                [(name, name) for name in sorted(self.app.available_themes)],
                value=settings.theme,
                allow_blank=False,
                id="setting-theme",
            )
            for key, value in asdict(settings.thresholds).items():
                yield Label(
                    {
                        "cpu_busy": "Host CPU busy notice (%) with high load",
                        "memory_available": "Host RAM available warning below (%)",
                        "gpu_warm": "GPU warm notice (°C)",
                        "gpu_hot": "GPU hot notice (°C)",
                        "disk_used": "Filesystem used warning (%)",
                    }[key]
                )
                yield Input(str(value), id=f"setting-{key}")
            yield Static(Text(str(self.app.settings_path)), id="settings-message")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Save", id="settings-save", variant="primary")
                yield Button("Cancel", id="settings-cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "settings-cancel":
            self.dismiss(None)
            return
        if event.button.id != "settings-save":
            return
        try:
            value = lambda key: self.query_one(f"#setting-{key}", Input).value
            raw = {
                "refresh_interval": float(value("refresh_interval")),
                "default_panel": value("default_panel"),
                "hidden_interfaces": [
                    name.strip()
                    for name in value("hidden_interfaces").split(",")
                    if name.strip()
                ],
                "history_samples": int(value("history_samples")),
                "theme": self.query_one("#setting-theme", Select).value,
                "thresholds": {
                    key: float(value(key))
                    for key in asdict(self.app.settings.thresholds)
                },
            }
            settings = validate(raw)
            save(settings, self.app.settings_path)
            self.dismiss(settings)
        except (ValueError, OSError) as exc:
            self.query_one("#settings-message", Static).update(
                Text(str(exc), style="yellow")
            )

    def action_close(self):
        self.dismiss(None)


class ExportScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", show=False, priority=True)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Static("Export a fresh JSON snapshot", classes="dialog-title")
            yield Label("Destination (an existing file will not be overwritten)")
            yield Input(
                str(Path.cwd() / f"sysmon-report-{datetime.now():%Y%m%d-%H%M%S}.json"),
                id="export-path",
            )
            yield Checkbox(
                "Include process commands", value=False, id="export-commands"
            )
            yield Static(
                "Contains visible process names/users, metrics, mounts and socket addresses.\nCommands are omitted by default. The output file is readable only by its owner.",
                id="export-message",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Export", id="export-save", variant="primary")
                yield Button("Cancel", id="export-cancel")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "export-cancel":
            self.dismiss()
        elif event.button.id == "export-save":
            path = Path(self.query_one("#export-path", Input).value).expanduser()
            include = self.query_one("#export-commands", Checkbox).value
            event.button.disabled = True
            self.run_worker(self._export(path, include), exclusive=True)

    async def _export(self, path, include):
        try:
            report = await asyncio.to_thread(snapshot, include_commands=include)
            await asyncio.to_thread(write_report, report, path)
            self.app.notify(f"Saved {path}")
            self.dismiss()
        except (OSError, ValueError) as exc:
            self.query_one("#export-message", Static).update(
                Text(str(exc), style="yellow")
            )
            self.query_one("#export-save", Button).disabled = False

    def action_close(self):
        self.dismiss()
