"""Panel registry.

This is the extension point. To add a subsystem: write a source module under
``sources/``, write a ``Panel`` subclass under ``panels/``, and add it to
:data:`PANELS`. The tab bar, key routing and help overlay all read from here.
"""

from __future__ import annotations

from ..panels.cpu import CPUPanel
from ..panels.gpu import GPUPanel
from ..panels.memory import MemoryPanel
from ..panels.network import NetworkPanel
from ..panels.overview import OverviewPanel
from ..panels.processes import ProcessesPanel
from ..panels.disk import DiskPanel
from ..panels.resources import ResourcesPanel

#: Every panel, in tab-bar order. The first entry is the landing page.
PANELS: tuple[type, ...] = (
    OverviewPanel,
    CPUPanel,
    MemoryPanel,
    ProcessesPanel,
    GPUPanel,
    NetworkPanel,
    DiskPanel,
    ResourcesPanel,
)
