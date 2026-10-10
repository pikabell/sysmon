"""Minimal ctypes bindings for the NVIDIA Management Library (NVML).

This talks to ``libnvidia-ml.so.1`` directly, so there is no need for the CUDA
toolkit, a compiler, or a vendored NVML. Every call is optional: on a machine
without NVIDIA hardware the module simply reports itself unavailable and the GPU
panel degrades to "no devices".
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_char_p, c_uint, c_ulonglong, c_void_p
from dataclasses import dataclass

NVML_SUCCESS = 0
NVML_ERROR_NO_PERMISSION = 4
NVML_ERROR_INSUFFICIENT_SIZE = 7
NVML_ERROR_NOT_SUPPORTED = 3

NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2

_LIBRARIES = ("libnvidia-ml.so.1", "libnvidia-ml.so")


class _Utilization(Structure):
    _fields_ = [("gpu", c_uint), ("memory", c_uint)]


class _Memory(Structure):
    # Layout for the stable, unversioned nvmlDeviceGetMemoryInfo entry point.
    _fields_ = [("total", c_ulonglong), ("free", c_ulonglong), ("used", c_ulonglong)]


class _ProcEntry(Structure):
    _fields_ = [
        ("pid", c_uint),
        ("used_gpu_memory", c_ulonglong),
        ("gpu_instance_id", c_uint),
        ("compute_instance_id", c_uint),
    ]


class _ProcArray(Structure):
    _fields_ = [("version", c_uint), ("count", c_uint), ("procs", POINTER(_ProcEntry))]


@dataclass(frozen=True, slots=True)
class GPUDevice:
    index: int
    name: str
    util_gpu: int | None
    util_mem: int | None
    mem_used: int | None
    mem_total: int | None
    temperature: int | None
    power_w: float | None
    power_limit_w: float | None
    fan: int | None
    clock_mhz: int | None
    mem_clock_mhz: int | None


@dataclass(frozen=True, slots=True)
class GPUProcess:
    pid: int
    gpu_index: int
    kind: str  # "C" compute, "G" graphics
    mem_bytes: int | None
    sm_pct: float | None = None
    mem_pct: float | None = None


class NVML:
    """Thin wrapper over the handful of NVML entry points sysmon needs."""

    def __init__(self) -> None:
        self._lib: ctypes.CDLL | None = None
        self._handles: list[c_void_p] = []
        self.driver_version: str | None = None
        self.error: str | None = None
        # Set once a driver tells us it does not implement the process queries.
        self.procs_supported = True
        self._load()

    # -- setup -----------------------------------------------------------

    def _load(self) -> None:
        for candidate in _LIBRARIES:
            try:
                lib = ctypes.CDLL(candidate)
            except OSError:
                continue
            init = getattr(lib, "nvmlInit_v2", None)
            if init is None or init() != NVML_SUCCESS:
                self.error = "nvmlInit failed"
                continue
            self._lib = lib
            break
        else:
            self.error = "libnvidia-ml not found"
            self.procs_supported = False
            return

        # The length argument is mandatory; without an explicit signature ctypes
        # passes garbage for it and the call fails with NOT_SUPPORTED.
        version = ctypes.create_string_buffer(96)
        version_func = getattr(self._lib, "nvmlSystemGetDriverVersion", None)
        if version_func is not None:
            version_func.argtypes = [c_char_p, c_uint]
            version_func.restype = c_uint
        if self._call("nvmlSystemGetDriverVersion", version, 96) == NVML_SUCCESS:
            self.driver_version = version.value.decode("utf-8", "replace")

        count = c_uint()
        if self._call("nvmlDeviceGetCount_v2", byref(count)) != NVML_SUCCESS:
            self.error = "nvmlDeviceGetCount failed"
            self._lib = None
            self.procs_supported = False
            return

        for index in range(count.value):
            handle = c_void_p()
            if (
                self._call("nvmlDeviceGetHandleByIndex_v2", index, byref(handle))
                != NVML_SUCCESS
            ):
                break
            self._handles.append(handle)

    @property
    def available(self) -> bool:
        return self._lib is not None and bool(self._handles)

    @property
    def device_count(self) -> int:
        return len(self._handles)

    # -- per-device telemetry --------------------------------------------

    def _call(self, name: str, *args) -> int:
        function = getattr(self._lib, name, None)
        return NVML_ERROR_NOT_SUPPORTED if function is None else function(*args)

    def _name(self, handle: c_void_p) -> str:
        buf = ctypes.create_string_buffer(96)
        if self._call("nvmlDeviceGetName", handle, buf, 96) == NVML_SUCCESS:
            return buf.value.decode("utf-8", "replace")
        return "GPU"

    def _util(self, handle: c_void_p) -> tuple[int | None, int | None]:
        value = _Utilization()
        if (
            self._call("nvmlDeviceGetUtilizationRates", handle, byref(value))
            != NVML_SUCCESS
        ):
            return (None, None)
        return (int(value.gpu), int(value.memory))

    def _memory(self, handle: c_void_p) -> tuple[int | None, int | None]:
        value = _Memory()
        if self._call("nvmlDeviceGetMemoryInfo", handle, byref(value)) != NVML_SUCCESS:
            return (None, None)
        return (int(value.used), int(value.total))

    def _temperature(self, handle: c_void_p) -> int | None:
        value = c_uint()
        if (
            self._call(
                "nvmlDeviceGetTemperature", handle, NVML_TEMPERATURE_GPU, byref(value)
            )
            == NVML_SUCCESS
        ):
            return int(value.value)
        return None

    def _power(self, handle: c_void_p) -> tuple[float | None, float | None]:
        milli = c_uint()
        limit = c_uint()
        used = self._call("nvmlDeviceGetPowerUsage", handle, byref(milli))
        capped = self._call("nvmlDeviceGetEnforcedPowerLimit", handle, byref(limit))
        return (
            round(milli.value / 1000.0, 1) if used == NVML_SUCCESS else None,
            round(limit.value / 1000.0, 1) if capped == NVML_SUCCESS else None,
        )

    def _fan(self, handle: c_void_p) -> int | None:
        value = c_uint()
        if self._call("nvmlDeviceGetFanSpeed", handle, byref(value)) != NVML_SUCCESS:
            return None
        return None if value.value == 0xFFFFFFFF else int(value.value)

    def _clock(self, handle: c_void_p, kind: int) -> int | None:
        value = c_uint()
        if (
            self._call("nvmlDeviceGetClockInfo", handle, kind, byref(value))
            != NVML_SUCCESS
        ):
            return None
        return int(value.value)

    def devices(self) -> list[GPUDevice]:
        if not self.available:
            return []
        out: list[GPUDevice] = []
        for index, handle in enumerate(self._handles):
            util_gpu, util_mem = self._util(handle)
            mem_used, mem_total = self._memory(handle)
            power, limit = self._power(handle)
            out.append(
                GPUDevice(
                    index=index,
                    name=self._name(handle),
                    util_gpu=util_gpu,
                    util_mem=util_mem,
                    mem_used=mem_used,
                    mem_total=mem_total,
                    temperature=self._temperature(handle),
                    power_w=power,
                    power_limit_w=limit,
                    fan=self._fan(handle),
                    clock_mhz=self._clock(handle, NVML_CLOCK_GRAPHICS),
                    mem_clock_mhz=self._clock(handle, NVML_CLOCK_MEM),
                )
            )
        return out

    # -- per-process ------------------------------------------------------

    def _proc_function(self, base: str):
        # v2 and v3 share a layout for the fields we read; prefer the newer.
        for suffix in ("_v3", "_v2"):
            func = getattr(
                self._lib, f"nvmlDeviceGet{base}RunningProcesses{suffix}", None
            )
            if func is not None:
                return func
        return None

    def processes(self) -> list[GPUProcess] | None:
        """Per-process GPU usage, or ``None`` when the driver does not support it.

        Unsupported queries are cached. A size query returning INSUFFICIENT_SIZE
        requests an allocation, not a fallback. Other failures remain retryable.
        """
        if not self.available or not self.procs_supported:
            return None

        collected: dict[tuple[int, int], GPUProcess] = {}
        for index, handle in enumerate(self._handles):
            for kind, base in (("C", "Compute"), ("G", "Graphics")):
                func = self._proc_function(base)
                if func is None:
                    self.procs_supported = False
                    return None
                count = c_uint()
                code = func(handle, byref(count), None)
                if code == NVML_ERROR_NOT_SUPPORTED:
                    self.procs_supported = False
                    return None
                if code not in (NVML_SUCCESS, NVML_ERROR_INSUFFICIENT_SIZE):
                    return None
                if count.value == 0:
                    continue
                for _ in range(3):
                    capacity = count.value + 8
                    buffer = (_ProcEntry * capacity)()
                    count.value = capacity
                    code = func(handle, byref(count), buffer)
                    if code != NVML_ERROR_INSUFFICIENT_SIZE:
                        break
                if code != NVML_SUCCESS or count.value > capacity:
                    return None
                for slot in range(count.value):
                    entry = buffer[slot]
                    key = (int(entry.pid), index)
                    previous = collected.get(key)
                    memory = (
                        None
                        if entry.used_gpu_memory == 2**64 - 1
                        else int(entry.used_gpu_memory)
                    )
                    if previous is not None:
                        known = [
                            value
                            for value in (previous.mem_bytes, memory)
                            if value is not None
                        ]
                        memory = max(known) if known else None
                    collected[key] = GPUProcess(
                        pid=int(entry.pid),
                        gpu_index=index,
                        kind=kind if previous is None else "C+G",
                        mem_bytes=memory,
                    )
        return list(collected.values())


__all__ = [
    "NVML",
    "GPUDevice",
    "GPUProcess",
    "NVML_SUCCESS",
    "NVML_ERROR_NOT_SUPPORTED",
]
