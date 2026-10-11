"""Validated JSON settings. Loading settings never creates files."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Thresholds:
    cpu_busy: float = 92.0
    memory_available: float = 5.0
    gpu_warm: float = 78.0
    gpu_hot: float = 83.0
    disk_used: float = 90.0


@dataclass(frozen=True, slots=True)
class Settings:
    refresh_interval: float = 1.0
    default_panel: str = "0"
    hidden_interfaces: tuple[str, ...] = ()
    theme: str = "sysmon-dark"
    history_samples: int = 600
    thresholds: Thresholds = field(default_factory=Thresholds)


def config_path() -> Path:
    # XDG paths must be absolute; empty/relative values mean use the default.
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not directory.is_absolute():
        directory = Path.home() / ".config"
    return directory / "sysmon/config.json"


def _number(value, message: str) -> float:
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc


def validate(raw: dict) -> Settings:
    if not isinstance(raw, dict):
        raise ValueError("settings must be a JSON object")
    unknown = set(raw) - set(Settings.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
    interval = _number(
        raw.get("refresh_interval", 1), "refresh interval must be 0.2–60 seconds"
    )
    if not math.isfinite(interval) or not 0.2 <= interval <= 60:
        raise ValueError("refresh interval must be 0.2–60 seconds")
    panel = str(raw.get("default_panel", "0"))
    if panel not in "01234567" or len(panel) != 1:
        raise ValueError("default panel must be 0–7")
    hidden = raw.get("hidden_interfaces", ())
    if not isinstance(hidden, (list, tuple)) or any(
        not isinstance(item, str) or not item or len(item) > 128 for item in hidden
    ):
        raise ValueError("hidden_interfaces must be a list of interface names")
    theme = raw.get("theme", "sysmon-dark")
    if not isinstance(theme, str) or not theme:
        raise ValueError("theme must be a name")
    samples = raw.get("history_samples", 600)
    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or not 10 <= samples <= 3600
    ):
        raise ValueError("history_samples must be an integer from 10 to 3600")
    thresholds = raw.get("thresholds", {})
    if not isinstance(thresholds, dict) or set(thresholds) - set(
        Thresholds.__dataclass_fields__
    ):
        raise ValueError("unknown warning threshold")
    values = asdict(Thresholds())
    for key, value in thresholds.items():
        limit = 150 if key.startswith("gpu_") else 100
        value = _number(value, f"{key} must be between 0 and {limit}")
        if not math.isfinite(value) or not 0 <= value <= limit:
            raise ValueError(f"{key} must be between 0 and {limit}")
        values[key] = value
    if values["gpu_warm"] > values["gpu_hot"]:
        raise ValueError("GPU warm threshold must not exceed hot threshold")
    return Settings(
        interval,
        panel,
        tuple(dict.fromkeys(hidden)),
        theme,
        samples,
        Thresholds(**values),
    )


def load(path: Path | None = None) -> Settings:
    path = path if path is not None else config_path()
    try:
        return validate(json.loads(path.read_text()))
    except FileNotFoundError:
        return Settings()
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid settings: {exc}") from exc


def save(settings: Settings, path: Path) -> None:
    validate(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".sysmon-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(asdict(settings), handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
