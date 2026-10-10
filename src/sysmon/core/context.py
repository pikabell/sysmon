"""Shared data sources.

Every panel needs the same few subsystems, and several of them are expensive:
a full process sweep is ~26 ms and the ``nvidia-smi pmon`` fallback is ~350 ms.
Creating a private source per panel would mean re-running that work every time you
switch tabs, and would double the cost on the overview, which needs all of them at
once.

So the sources are created once, lazily, and shared. That also gives the caches
inside them (``cmdline`` per pid, the pmon result, interface rate deltas) a single
owner, which is what makes them effective.

Rate-based sources need two samples before they have anything to report, so
:meth:`Sources.warm` takes a throwaway first sample. The app calls it on a worker
thread at startup, which is why the first painted frame shows real CPU and network
numbers instead of a screen full of zeroes.
"""

from __future__ import annotations

import threading

from ..sources.cpu import CPUSource
from ..sources.gpu import GPUSource
from ..sources.memory import MemSource
from ..sources.net import NetSource
from ..sources.procs import ProcSource
from ..sources.disk import DiskSource
from ..sources.resources import ResourceSource


class Sources:
    """Lazily-constructed, shared, and warmable.

    Uses :class:`threading.RLock` because two properties (``net`` and ``gpu``) must
    resolve ``proc`` while already inside the lock; a plain Lock deadlocks there.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._warmed = False
        self._proc: ProcSource | None = None
        self._cpu: CPUSource | None = None
        self._mem: MemSource | None = None
        self._net: NetSource | None = None
        self._gpu: GPUSource | None = None
        self._disk: DiskSource | None = None
        self._resources: ResourceSource | None = None

    # -- lazily created sources -------------------------------------------

    @property
    def proc(self) -> ProcSource:
        with self._lock:
            if self._proc is None:
                self._proc = ProcSource()
            return self._proc

    @property
    def cpu(self) -> CPUSource:
        with self._lock:
            if self._cpu is None:
                self._cpu = CPUSource()
            return self._cpu

    @property
    def mem(self) -> MemSource:
        with self._lock:
            if self._mem is None:
                self._mem = MemSource()
            return self._mem

    @property
    def net(self) -> NetSource:
        # NetSource resolves socket owners through the process source.
        proc = self.proc
        with self._lock:
            if self._net is None:
                self._net = NetSource(proc)
            return self._net

    @property
    def gpu(self) -> GPUSource:
        # Same re-entrancy concern as `net`: resolve proc first, lock second.
        proc = self.proc
        with self._lock:
            if self._gpu is None:
                self._gpu = GPUSource(proc)
            return self._gpu

    # -- warm-up -----------------------------------------------------------

    @property
    def disk(self) -> DiskSource:
        with self._lock:
            if self._disk is None:
                self._disk = DiskSource()
            return self._disk

    @property
    def resources(self) -> ResourceSource:
        with self._lock:
            if self._resources is None:
                self._resources = ResourceSource()
            return self._resources

    @property
    def warmed(self) -> bool:
        return self._warmed

    def warm(self) -> None:
        with self._lock:
            self._warm()

    def collect(self, fetch):
        """Serialize shared sampling, including threads left by cancelled workers."""
        with self._lock:
            return fetch()

    def _warm(self) -> None:
        """Take one throwaway sample of each source to establish baselines.

        Must run off the event loop: the GPU path may spawn ``nvidia-smi``.
        """
        if self._warmed:
            return
        for sample in (
            self.cpu.sample,
            self.mem.sample,
            self.proc.sample,
            self.net.sample,
            self.gpu.sample,
            self.disk.sample,
            self.resources.sample,
        ):
            try:
                sample()
            except Exception:
                # A missing subsystem must not prevent the others from warming.
                continue
        self._warmed = True


#: The process-wide instance every panel shares.
SOURCES = Sources()
