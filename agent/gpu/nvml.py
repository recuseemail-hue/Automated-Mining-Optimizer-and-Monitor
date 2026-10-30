"""NVML backend via nvidia-ml-py (official bindings).

Known gaps (see docs/sensors.md): no fan RPM, no GDDR6X memory-junction temperature on GeForce,
fan control and VF offsets need Administrator/root and a recent driver; fan control on Windows
GeForce may be refused by NVML entirely (verify with `rigctl sensors`).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import pynvml as N

from .base import GpuBackend, GpuInfo, GpuSample, NotSupported

log = logging.getLogger(__name__)

_THROTTLE_BITS: list[tuple[str, int]] = []
for _name, _label in (
    ("nvmlClocksThrottleReasonSwPowerCap", "power_cap"),
    ("nvmlClocksThrottleReasonHwSlowdown", "hw_slowdown"),
    ("nvmlClocksThrottleReasonSwThermalSlowdown", "sw_thermal"),
    ("nvmlClocksThrottleReasonHwThermalSlowdown", "hw_thermal"),
    ("nvmlClocksThrottleReasonHwPowerBrakeSlowdown", "power_brake"),
    ("nvmlClocksThrottleReasonSyncBoost", "sync_boost"),
    ("nvmlClocksThrottleReasonApplicationsClocksSetting", "app_clocks"),
    ("nvmlClocksThrottleReasonDisplayClockSetting", "display_clocks"),
):
    if hasattr(N, _name):
        _THROTTLE_BITS.append((_label, getattr(N, _name)))


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


class NvmlBackend(GpuBackend):
    name = "nvml"

    def __init__(self) -> None:
        N.nvmlInit()
        self._handles: dict[str, Any] = {}
        self._infos: dict[str, GpuInfo] = {}
        self.driver = _s(N.nvmlSystemGetDriverVersion())
        self.enumerate()

    def close(self) -> None:
        try:
            N.nvmlShutdown()
        except Exception:
            pass

    @staticmethod
    def _try(fn: Callable, *args: Any, default: Any = None) -> Any:
        try:
            return fn(*args)
        except N.NVMLError:
            return default

    def _h(self, uuid: str) -> Any:
        if uuid not in self._handles:
            self.enumerate()
        if uuid not in self._handles:
            raise NotSupported(f"GPU {uuid} not present")
        return self._handles[uuid]

    # --- discovery --------------------------------------------------------------
    def enumerate(self) -> list[GpuInfo]:
        infos: list[GpuInfo] = []
        handles: dict[str, Any] = {}
        for i in range(N.nvmlDeviceGetCount()):
            h = N.nvmlDeviceGetHandleByIndex(i)
            uuid = _s(N.nvmlDeviceGetUUID(h))
            pci = self._try(N.nvmlDeviceGetPciInfo, h)
            cons = self._try(N.nvmlDeviceGetPowerManagementLimitConstraints, h)
            mem = self._try(N.nvmlDeviceGetMemoryInfo, h)
            info = GpuInfo(
                uuid=uuid,
                index=i,
                name=_s(N.nvmlDeviceGetName(h)),
                bus_id=_s(pci.busId) if pci is not None else None,
                stock_pl_w=self._mw(self._try(N.nvmlDeviceGetPowerManagementDefaultLimit, h)),
                min_pl_w=self._mw(cons[0]) if cons else None,
                max_pl_w=self._mw(cons[1]) if cons else None,
                vram_mb=int(mem.total / 2**20) if mem is not None else None,
            )
            infos.append(info)
            handles[uuid] = h
        self._handles, self._infos = handles, {i.uuid: i for i in infos}
        return infos

    @staticmethod
    def _mw(v: Any) -> float | None:
        return None if v is None else float(v) / 1000.0

    # --- sampling ---------------------------------------------------------------
    def sample(self, uuid: str) -> GpuSample:
        h = self._h(uuid)
        s = GpuSample(ts=time.time())
        s.power_w = self._mw(self._try(N.nvmlDeviceGetPowerUsage, h))
        s.core_c = self._try(N.nvmlDeviceGetTemperature, h, N.NVML_TEMPERATURE_GPU)
        s.memj_c = self._mem_temp(h)
        s.fan_pct = self._try(N.nvmlDeviceGetFanSpeed, h)
        s.core_mhz = self._try(N.nvmlDeviceGetClockInfo, h, N.NVML_CLOCK_GRAPHICS)
        s.mem_mhz = self._try(N.nvmlDeviceGetClockInfo, h, N.NVML_CLOCK_MEM)
        s.pl_w = self._mw(self._try(N.nvmlDeviceGetPowerManagementLimit, h))
        util = self._try(N.nvmlDeviceGetUtilizationRates, h)
        s.util_pct = util.gpu if util is not None else None
        mem = self._try(N.nvmlDeviceGetMemoryInfo, h)
        s.vram_used_mb = mem.used / 2**20 if mem is not None else None
        if hasattr(N, "nvmlDeviceGetGpcClkVfOffset"):
            s.core_offset_mhz = self._try(N.nvmlDeviceGetGpcClkVfOffset, h)
        if hasattr(N, "nvmlDeviceGetMemClkVfOffset"):
            s.mem_offset_mhz = self._try(N.nvmlDeviceGetMemClkVfOffset, h)
        s.throttle = self._throttle(h)
        pstate = self._try(N.nvmlDeviceGetPerformanceState, h)
        if pstate is not None:
            s.extra["pstate"] = int(pstate)
        return s

    def _mem_temp(self, h: Any) -> float | None:
        # NVML_FI_DEV_MEMORY_TEMP works on HBM datacenter parts; GeForce GDDR6X normally returns NotSupported.
        fid = getattr(N, "NVML_FI_DEV_MEMORY_TEMP", None)
        if fid is None:
            return None
        vals = self._try(N.nvmlDeviceGetFieldValues, h, [fid])
        if not vals:
            return None
        v = vals[0]
        if getattr(v, "nvmlReturn", 1) != 0:
            return None
        return float(v.value.uiVal)

    def _throttle(self, h: Any) -> list[str]:
        fn = getattr(N, "nvmlDeviceGetCurrentClocksEventReasons", None) or getattr(
            N, "nvmlDeviceGetCurrentClocksThrottleReasons", None)
        if fn is None:
            return []
        mask = self._try(fn, h)
        if not mask:
            return []
        return [label for label, bit in _THROTTLE_BITS if mask & bit]

    # --- capability probe (read-only; no setting is changed) ---------------------
    def capabilities(self, uuid: str) -> dict[str, bool | str]:
        h = self._h(uuid)
        caps: dict[str, bool | str] = {"driver": self.driver}
        caps["power_limit_read"] = self._try(N.nvmlDeviceGetPowerManagementLimit, h) is not None
        caps["power_limit_constraints"] = self._try(N.nvmlDeviceGetPowerManagementLimitConstraints, h) is not None
        caps["memj_nvml_field"] = self._mem_temp(h) is not None
        caps["fan_read"] = self._try(N.nvmlDeviceGetFanSpeed, h) is not None
        caps["num_fans"] = self._try(N.nvmlDeviceGetNumFans, h, default=0)
        caps["core_offset_read"] = hasattr(N, "nvmlDeviceGetGpcClkVfOffset") and \
            self._try(N.nvmlDeviceGetGpcClkVfOffset, h) is not None
        caps["mem_offset_read"] = hasattr(N, "nvmlDeviceGetMemClkVfOffset") and \
            self._try(N.nvmlDeviceGetMemClkVfOffset, h) is not None
        caps["fan_set_api_present"] = hasattr(N, "nvmlDeviceSetFanSpeed_v2")
        caps["locked_clocks_api_present"] = hasattr(N, "nvmlDeviceSetGpuLockedClocks")
        # Write capability can only be proven by a round-trip set; `rigctl sensors --probe-write`
        # does a no-op write (current value -> current value) when control is enabled for the GPU.
        return caps

    def probe_write(self, uuid: str) -> dict[str, str]:
        """No-op writes: set each control to its current value and report NVML's answer."""
        h = self._h(uuid)
        out: dict[str, str] = {}
        cur_pl = self._try(N.nvmlDeviceGetPowerManagementLimit, h)
        try:
            N.nvmlDeviceSetPowerManagementLimit(h, cur_pl)
            out["power_limit"] = "ok"
        except N.NVMLError as e:
            out["power_limit"] = str(e)
        for label, getter, setter in (
            ("core_offset", "nvmlDeviceGetGpcClkVfOffset", "nvmlDeviceSetGpcClkVfOffset"),
            ("mem_offset", "nvmlDeviceGetMemClkVfOffset", "nvmlDeviceSetMemClkVfOffset"),
        ):
            if not (hasattr(N, getter) and hasattr(N, setter)):
                out[label] = "api missing in bindings"
                continue
            cur = self._try(getattr(N, getter), h)
            if cur is None:
                out[label] = "read unsupported"
                continue
            try:
                getattr(N, setter)(h, int(cur))
                out[label] = "ok"
            except N.NVMLError as e:
                out[label] = str(e)
        if hasattr(N, "nvmlDeviceSetFanSpeed_v2"):
            cur = self._try(N.nvmlDeviceGetFanSpeed_v2, h, 0)
            if cur is None:
                out["fan"] = "read unsupported"
            else:
                try:
                    N.nvmlDeviceSetFanSpeed_v2(h, 0, int(cur))
                    N.nvmlDeviceSetDefaultFanSpeed_v2(h, 0)   # hand control straight back to the driver
                    out["fan"] = "ok (restored to auto)"
                except N.NVMLError as e:
                    out["fan"] = str(e)
        else:
            out["fan"] = "api missing in bindings"
        return out

    # --- control ---------------------------------------------------------------
    def set_power_limit_w(self, uuid: str, watts: float) -> None:
        self._raise(N.nvmlDeviceSetPowerManagementLimit, self._h(uuid), int(watts * 1000))

    def set_locked_core_clock(self, uuid: str, min_mhz: int, max_mhz: int) -> None:
        self._raise(N.nvmlDeviceSetGpuLockedClocks, self._h(uuid), int(min_mhz), int(max_mhz))

    def reset_locked_core_clock(self, uuid: str) -> None:
        self._raise(N.nvmlDeviceResetGpuLockedClocks, self._h(uuid))

    def set_core_offset_mhz(self, uuid: str, mhz: int) -> None:
        if not hasattr(N, "nvmlDeviceSetGpcClkVfOffset"):
            raise NotSupported("core offset: nvml binding lacks nvmlDeviceSetGpcClkVfOffset")
        self._raise(N.nvmlDeviceSetGpcClkVfOffset, self._h(uuid), int(mhz))

    def set_mem_offset_mhz(self, uuid: str, mhz: int) -> None:
        if not hasattr(N, "nvmlDeviceSetMemClkVfOffset"):
            raise NotSupported("mem offset: nvml binding lacks nvmlDeviceSetMemClkVfOffset")
        self._raise(N.nvmlDeviceSetMemClkVfOffset, self._h(uuid), int(mhz))

    def set_fan_pct(self, uuid: str, pct: int) -> None:
        h = self._h(uuid)
        n = self._try(N.nvmlDeviceGetNumFans, h, default=1) or 1
        for f in range(n):
            self._raise(N.nvmlDeviceSetFanSpeed_v2, h, f, int(pct))

    def set_fan_auto(self, uuid: str) -> None:
        h = self._h(uuid)
        n = self._try(N.nvmlDeviceGetNumFans, h, default=1) or 1
        for f in range(n):
            self._raise(N.nvmlDeviceSetDefaultFanSpeed_v2, h, f)

    @staticmethod
    def _raise(fn: Callable, *args: Any) -> Any:
        try:
            return fn(*args)
        except N.NVMLError as e:
            raise NotSupported(f"{fn.__name__}: {e}") from e
