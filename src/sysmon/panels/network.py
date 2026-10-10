"""Network panel: interface throughput and live socket ownership."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ..core.context import SOURCES
from ..core.panel import Panel
from ..core.render import human_bytes, human_count, human_rate
from ..core.table import TableSync
from ..sources.net import NetSnapshot

IFACE_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("name", "INTERFACE", 16),
    ("rx", "RX RATE", 12),
    ("tx", "TX RATE", 12),
    ("rxpps", "RX PKT/s", 9),
    ("txpps", "TX PKT/s", 9),
    ("total", "TOTAL RX / TX", 22),
    ("err", "ERRORS", 9),
)

CONN_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("proto", "PROTO", 5),
    ("local", "LOCAL ADDRESS", 24),
    ("remote", "REMOTE ADDRESS", 30),
    ("status", "STATE", 12),
    ("pid", "PID", 8),
    ("user", "USER", 10),
    ("proc", "PROCESS", None),
)

# Colour the states that matter when something is misbehaving.
STATE_STYLES = {
    "ESTABLISHED": "green",
    "LISTEN": "bold cyan",
    "TIME_WAIT": "grey58",
    "CLOSE_WAIT": "yellow",
    "SYN_SENT": "yellow",
    "SYN_RECV": "yellow",
}


class NetworkPanel(Panel):
    KEY = "5"
    TITLE = "Network"
    INTERVAL = 1.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source = SOURCES.net
        self._iface_sync: TableSync | None = None
        self._conn_sync: TableSync | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="net-header")
            yield DataTable(id="net-ifaces", zebra_stripes=True)
            yield Static(id="net-note")
            yield DataTable(id="net-conns", zebra_stripes=True, cursor_type="row")
        yield Static("", id="status", classes="statusline")

    def on_mount(self) -> None:
        ifaces = self.query_one("#net-ifaces", DataTable)
        for key, label, width in IFACE_COLUMNS:
            ifaces.add_column(label, key=key, width=width)
        self._iface_sync = TableSync(ifaces, [(k, l) for k, l, _ in IFACE_COLUMNS])

        conns = self.query_one("#net-conns", DataTable)
        for key, label, width in CONN_COLUMNS:
            conns.add_column(label, key=key, width=width)
        self._conn_sync = TableSync(conns, [(k, l) for k, l, _ in CONN_COLUMNS])

    def fetch(self) -> NetSnapshot:
        return self.source.sample()

    def apply(self, snapshot: NetSnapshot) -> None:
        from dataclasses import replace

        hidden = self.settings.hidden_interfaces
        snapshot = replace(
            snapshot,
            ifaces=tuple(
                iface for iface in snapshot.ifaces if iface.name not in hidden
            ),
        )
        self.query_one("#net-header", Static).update(self._header(snapshot))
        if self._iface_sync is not None:
            ifaces = {iface.name: self._iface_cells(iface) for iface in snapshot.ifaces}
            # The interface set is stable, so order by name and update in place.
            self._iface_sync.sync(ifaces, sorted(ifaces))

        note = Text()
        note.append(
            snapshot.error
            or "Visible sockets; access to owners depends on permissions",
            style="grey62",
        )
        self.query_one("#net-note", Static).update(note)

        if self._conn_sync is not None:
            rows = {key: self._conn_cells(conn) for key, conn in _index_conns(snapshot)}
            self._conn_sync.sync(rows, list(rows))

    def status_prefix(self) -> str:
        return ""

    # -- rendering ---------------------------------------------------------

    def _header(self, snapshot: NetSnapshot) -> Text:
        text = Text()
        text.append("interfaces", style="bold grey58")
        text.append("  ·  ", style="grey35")
        text.append(f"{len(snapshot.ifaces)} present", style="grey62")
        text.append("  ·  ", style="grey35")
        text.append(f"{len(snapshot.conns)} sockets", style="grey62")
        return text

    def _iface_cells(self, iface) -> tuple:
        errors = iface.rx_errors + iface.tx_errors
        return (
            Text(iface.name, style="bold"),
            Text(human_rate(iface.rx_rate), style="cyan"),
            Text(human_rate(iface.tx_rate), style="magenta"),
            human_count(iface.rx_pps),
            human_count(iface.tx_pps),
            Text(human_bytes(iface.rx_bytes), style="cyan")
            + Text(" / ", style="grey35")
            + Text(human_bytes(iface.tx_bytes), style="magenta"),
            Text(str(errors) if errors else "-", style="red" if errors else "grey42"),
        )

    @staticmethod
    def _conn_cells(conn) -> tuple:
        return (
            Text(conn.proto, style="cyan" if conn.proto == "tcp" else "magenta"),
            conn.local,
            conn.remote or Text("-", style="grey42"),
            Text(conn.status, style=STATE_STYLES.get(conn.status, "grey70")),
            str(conn.pid) if conn.pid else Text("-", style="grey42"),
            Text(conn.username) if conn.username else Text("-", style="grey42"),
            Text(conn.process) if conn.process else Text("-", style="grey42"),
        )


def _index_conns(snapshot: NetSnapshot):
    """Yield ``(stable_key, conn)`` pairs.

    Sockets are ephemeral, so the key includes the row's position: it stays stable
    across ticks as long as the ordering is stable, which keeps the table from
    flickering, and it stays unique when two sockets share a tuple of fields.
    """
    for index, conn in enumerate(snapshot.conns):
        yield (conn.proto, conn.local, conn.remote, conn.pid, index), conn
