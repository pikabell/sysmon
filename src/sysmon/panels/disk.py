"""Disk activity and mount capacity. Partitions are shown, never summed twice."""

from rich.text import Text
from textual.widgets import DataTable, Static
from textual.app import ComposeResult

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import human_bytes, human_rate
from ..core.table import TableSync


class DiskPanel(Panel):
    KEY = "6"
    TITLE = "Disk"

    def compose(self) -> ComposeResult:
        yield Static(
            "Device activity · busy time is not an SSD saturation measure",
            id="disk-note",
        )
        yield DataTable(id="disk-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            "Mount capacity · available excludes reserved blocks · sampled every 10s",
            id="mount-note",
        )
        yield DataTable(id="mount-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status", classes="statusline")

    def on_mount(self):
        disks = [
            ("device", "DEVICE"),
            ("read", "READ/s"),
            ("write", "WRITE/s"),
            ("reads", "READ IOPS"),
            ("writes", "WRITE IOPS"),
            ("busy", "BUSY%"),
            ("queue", "IN FLIGHT"),
            ("kind", "KIND"),
        ]
        mounts = [
            ("mount", "MOUNT"),
            ("fs", "FS"),
            ("total", "SIZE"),
            ("used", "USED"),
            ("available", "AVAILABLE"),
            ("percent", "USED%"),
            ("inodes", "INODES%"),
            ("note", "NOTE"),
        ]
        for identifier, columns in (("disk-table", disks), ("mount-table", mounts)):
            table = self.query_one(f"#{identifier}", DataTable)
            for key, label in columns:
                table.add_column(label, key=key)
        self._disks = TableSync(self.query_one("#disk-table", DataTable), disks)
        self._mounts = TableSync(self.query_one("#mount-table", DataTable), mounts)

    def fetch(self):
        return SOURCES.disk.sample()

    def apply(self, snapshot):
        def number(value):
            return "-" if value is None else f"{value:.1f}"

        rows = {
            disk.name: (
                Text(disk.name),
                Text(human_rate(disk.read_rate), style="cyan", justify="right"),
                Text(human_rate(disk.write_rate), style="magenta", justify="right"),
                Text(number(disk.reads_per_second), justify="right"),
                Text(number(disk.writes_per_second), justify="right"),
                Text(
                    number(None if disk.busy is None else disk.busy * 100),
                    justify="right",
                ),
                Text(str(disk.in_flight), justify="right"),
                "partition" if disk.partition else "device",
            )
            for disk in snapshot.devices
        }
        self._disks.sync(rows, sorted(rows))
        self.query_one("#disk-table", DataTable).styles.height = max(
            3, min(12, len(rows) + 2)
        )
        thresholds = self.settings.thresholds
        rows = {}
        for fs in snapshot.filesystems:
            fraction = (
                fs.used / (fs.used + fs.available)
                if fs.used is not None
                and fs.available is not None
                and fs.used + fs.available
                else None
            )
            percent = None if fraction is None else fraction * 100
            rows[fs.path] = (
                Text(fs.path),
                fs.fs,
                Text(human_bytes(fs.total), justify="right"),
                Text(human_bytes(fs.used), justify="right"),
                Text(human_bytes(fs.available), justify="right"),
                Text(
                    number(percent),
                    style="yellow"
                    if percent is not None and percent >= thresholds.disk_used
                    else "cyan",
                    justify="right",
                ),
                Text(
                    number(
                        fs.inode_used / fs.inode_total * 100 if fs.inode_total else None
                    ),
                    justify="right",
                ),
                Text(fs.error or ""),
            )
        self._mounts.sync(rows, sorted(rows))
        self.query_one("#disk-note", Static).update(
            Text(
                snapshot.error
                or (
                    f"Device activity · {len(snapshot.devices)} "
                    f"{'device' if len(snapshot.devices) == 1 else 'devices'} "
                    "· [i] metric definitions"
                )
            )
        )
