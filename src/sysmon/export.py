"""Explicit, private snapshot exports; commands omitted unless requested."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .core.context import SOURCES, Sources


def snapshot(sources: Sources = SOURCES, *, include_commands: bool = False) -> dict:
    def collect():
        result = {
            "schema_version": 1,
            "sysmon_version": __version__,
            "collected_at": time.time(),
            "cpu": asdict(sources.cpu.sample()),
            "memory": asdict(sources.mem.sample()),
            "resources": asdict(sources.resources.sample()),
            "disk": asdict(sources.disk.sample()),
            "network": asdict(sources.net.sample()),
            "gpu": asdict(sources.gpu.sample()),
            "processes": [asdict(proc) for proc in sources.proc.sample().values()],
        }
        if not include_commands:

            def redact(value):
                if isinstance(value, dict):
                    return {
                        key: redact(item)
                        for key, item in value.items()
                        if key not in {"cmdline", "command", "process"}
                    }
                if isinstance(value, (list, tuple)):
                    return [redact(item) for item in value]
                return value

            result = redact(result)
        return result

    return sources.collect(collect)


def write_report(report: dict, path: Path) -> None:
    # Never overwrite an existing report. The default mode is owner-only.
    content = json.dumps(report, indent=2, allow_nan=False) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
