"""Network throughput and live socket ownership.

Throughput comes from ``/proc/net/dev``. Sockets are read through psutil where
possible, falling back to parsing ``/proc/net/{tcp,udp}*`` and walking
``/proc/*/fd`` to attribute each socket inode to a process. The fallback exists
because mapping inodes through ``/proc/*/fd`` works unprivileged on Linux, while
psutil's connection enumeration is not uniformly readable as a normal user.
"""

from __future__ import annotations

import os
import pwd
import re
import socket
import struct
import time
from dataclasses import dataclass

try:
    import psutil
except ImportError:
    psutil = None

from .procs import ProcInfo, ProcSource

PROC = "/proc"
PROC_NET = f"{PROC}/net"

# /proc/net/tcp state column -> name, shared by the IPv4 and IPv6 tables.
TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}

_SOCKET_RE = re.compile(r"socket:\[(\d+)\]")

_TABLES = (("tcp", False), ("tcp6", True), ("udp", False), ("udp6", True))


@dataclass(frozen=True, slots=True)
class IfaceStats:
    name: str
    rx_bytes: int
    tx_bytes: int
    rx_rate: float
    tx_rate: float
    rx_pps: float
    tx_pps: float
    rx_errors: int
    tx_errors: int
    rx_error_delta: int = 0
    tx_error_delta: int = 0


@dataclass(frozen=True, slots=True)
class ConnInfo:
    proto: str
    local: str
    remote: str
    status: str
    pid: int | None
    username: str
    process: str


@dataclass(frozen=True, slots=True)
class NetSnapshot:
    ifaces: tuple[IfaceStats, ...]
    conns: tuple[ConnInfo, ...]
    degraded: bool
    error: str | None


def _endpoint(host: str | None, port: int | None) -> str:
    """Render an address as ``host:port``, using ``*`` for wildcard binds."""
    if not host or host in ("0.0.0.0", "::"):
        shown = "*"
    elif ":" in host:
        shown = f"[{host}]"
    else:
        shown = host
    return f"{shown}:{port}" if port else shown


def _decode_addr(hex_addr: str, ipv6: bool) -> str:
    """Decode a /proc/net hex address.

    Procfs encodes addresses as 32-bit words in native host byte order.
    """
    try:
        if ipv6:
            words = [hex_addr[i : i + 8] for i in range(0, 32, 8)]
            packed = b"".join(struct.pack("=I", int(word, 16)) for word in words)
            return socket.inet_ntop(socket.AF_INET6, packed)
        return socket.inet_ntoa(struct.pack("=I", int(hex_addr, 16)))
    except (ValueError, OSError, struct.error):
        return hex_addr


def _socket_owners() -> dict[str, int]:
    """Map ``socket:[inode]`` -> pid by scanning ``/proc/*/fd``."""
    owners: dict[str, int] = {}
    try:
        entries = list(os.scandir(PROC))
    except OSError:
        return owners
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = f"{entry.path}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                match = _SOCKET_RE.fullmatch(os.readlink(f"{fd_dir}/{fd}"))
            except OSError:
                continue
            if match:
                owners.setdefault(match.group(1), int(entry.name))
    return owners


class NetSource:
    """Samples interface counters and the socket table."""

    def __init__(self, proc_source: ProcSource) -> None:
        self._procs = proc_source
        self._prev: dict[str, tuple[int, int, int, int]] = {}
        self._prev_at: float | None = None
        self._users: dict[int, str] = {}
        self._prev_errors: dict[str, tuple[int, int]] = {}
        self._iface_error: str | None = None

    def _username(self, uid: int) -> str:
        name = self._users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            self._users[uid] = name
        return name

    # -- interfaces -------------------------------------------------------

    def _interfaces(self) -> tuple[IfaceStats, ...]:
        try:
            with open(f"{PROC_NET}/dev") as handle:
                lines = handle.read().splitlines()[2:]
        except OSError:
            self._iface_error = (
                "Interface counters unavailable: /proc/net/dev is not readable"
            )
            return ()

        self._iface_error = None

        now = time.monotonic()
        elapsed = None if self._prev_at is None else max(1e-6, now - self._prev_at)
        current: dict[str, tuple[int, int, int, int]] = {}
        stats: list[IfaceStats] = []
        error_counts: dict[str, tuple[int, int]] = {}

        for line in lines:
            name, separator, rest = line.rpartition(":")
            fields = rest.split()
            if not separator or len(fields) < 16:
                self._iface_error = (
                    "Some interface counters are malformed or incomplete"
                )
                continue
            name = name.strip()
            try:
                counters = [int(value) for value in fields[:16]]
                if not name or any(value < 0 for value in counters):
                    raise ValueError("invalid interface counters")
            except ValueError:
                self._iface_error = (
                    "Some interface counters are malformed or incomplete"
                )
                continue
            rx_bytes, rx_packets, rx_errors = counters[:3]
            tx_bytes, tx_packets, tx_errors = counters[8:11]
            prior_errors = self._prev_errors.get(name, (rx_errors, tx_errors))
            error_counts[name] = (rx_errors, tx_errors)
            current[name] = (rx_bytes, tx_bytes, rx_packets, tx_packets)

            prior = self._prev.get(name)
            rx_rate = tx_rate = rx_pps = tx_pps = 0.0
            if prior is not None and elapsed:
                rx_rate = max(0.0, (rx_bytes - prior[0]) / elapsed)
                tx_rate = max(0.0, (tx_bytes - prior[1]) / elapsed)
                rx_pps = max(0.0, (rx_packets - prior[2]) / elapsed)
                tx_pps = max(0.0, (tx_packets - prior[3]) / elapsed)

            stats.append(
                IfaceStats(
                    name=name,
                    rx_bytes=rx_bytes,
                    tx_bytes=tx_bytes,
                    rx_rate=rx_rate,
                    tx_rate=tx_rate,
                    rx_pps=rx_pps,
                    tx_pps=tx_pps,
                    rx_errors=rx_errors,
                    tx_errors=tx_errors,
                    rx_error_delta=max(0, rx_errors - prior_errors[0]),
                    tx_error_delta=max(0, tx_errors - prior_errors[1]),
                )
            )

        self._prev = current
        self._prev_errors = error_counts
        self._prev_at = now
        return tuple(stats)

    # -- sockets ----------------------------------------------------------

    def _describe(self, pid: int | None) -> tuple[str, str]:
        """Resolve a pid to ``(username, command)`` for socket attribution."""
        if not pid:
            return "", ""
        info: ProcInfo | None = self._procs.describe(pid)
        return (info.username, info.cmdline) if info is not None else ("", "")

    def _conns_psutil(self) -> list[ConnInfo] | None:
        if psutil is None:
            return None
        try:
            raw = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError, OSError, NotImplementedError):
            return None

        out: list[ConnInfo] = []
        for conn in raw:
            if not conn.laddr:
                continue
            username, command = self._describe(conn.pid)
            proto = "tcp" if conn.type == socket.SOCK_STREAM else "udp"
            out.append(
                ConnInfo(
                    proto=proto,
                    local=_endpoint(conn.laddr[0], conn.laddr[1]),
                    remote=_endpoint(conn.raddr[0], conn.raddr[1])
                    if conn.raddr
                    else "",
                    status=conn.status or "-",
                    pid=conn.pid,
                    username=username,
                    process=command,
                )
            )
        return out

    def _conns_proc(self) -> list[ConnInfo]:
        owners = _socket_owners()
        out: list[ConnInfo] = []
        for table, ipv6 in _TABLES:
            try:
                with open(f"{PROC_NET}/{table}") as handle:
                    lines = handle.read().splitlines()[1:]
            except OSError:
                continue
            proto = "tcp" if table.startswith("tcp") else "udp"
            for line in lines:
                fields = line.split()
                if len(fields) < 10:
                    continue
                try:
                    local_raw, local_port = fields[1].rsplit(":", 1)
                    remote_raw, remote_port = fields[2].rsplit(":", 1)
                    local_port, remote_port = int(local_port, 16), int(remote_port, 16)
                    if not (0 <= local_port <= 65535 and 0 <= remote_port <= 65535):
                        continue
                except ValueError:
                    continue
                pid = owners.get(fields[9])
                username, command = self._describe(pid)
                out.append(
                    ConnInfo(
                        proto=proto,
                        local=_endpoint(_decode_addr(local_raw, ipv6), local_port),
                        remote=_endpoint(_decode_addr(remote_raw, ipv6), remote_port),
                        status=TCP_STATES.get(fields[3], fields[3])
                        if proto == "tcp"
                        else "-",
                        pid=pid,
                        username=username,
                        process=command,
                    )
                )
        return out

    def sample(self) -> NetSnapshot:
        ifaces = self._interfaces()
        conns = self._conns_psutil()
        degraded = conns is None
        if degraded:
            conns = self._conns_proc()
        # Listening sockets first, then established, then the rest.
        conns.sort(key=lambda c: (c.proto != "tcp", c.status != "LISTEN", c.local))
        return NetSnapshot(
            ifaces=ifaces,
            conns=tuple(conns),
            degraded=degraded,
            error=self._iface_error
            or ("Socket ownership may be limited by permissions" if degraded else None),
        )
