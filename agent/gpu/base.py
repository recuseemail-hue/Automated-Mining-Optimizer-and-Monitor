from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


class NotSupported(Exception):
    """The driver/OS does not expose this sensor or control for this GPU."""


@dataclass
class GpuInfo:
    uuid: str
    index: int
    name: str
    bus_id: str | None
    stock_pl_w: float | None
    min_pl_w: float | None
    max_pl_w: float | None
    vram_mb: int | None


@dataclass
class GpuSample:
    ts: float
    power_w: float | None = None
    core_c: float | None = None
    memj_c: float | None = None
    fan_pct: float | None = None
    fan_rpm: float | None = None
    core_mhz: float | None = None
    mem_mhz: float | None = None
    pl_w: float | None = None
    util_pct: float | None = None
    vram_used_mb: float | None = None
    core_offset_mhz: float | None = None
    mem_offset_mhz: float | None = None
    throttle: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_cols(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("ts")
        return d


class GpuBackend:
    """Interface. Control methods raise NotSupported when the platform cannot do it."""

    name = "base"

    def enumerate(self) -> list[GpuInfo]:
        raise NotImplementedError

    def sample(self, uuid: str) -> GpuSample:
        raise NotImplementedError

    def capabilities(self, uuid: str) -> dict[str, bool | str]:
        """Probe which controls work. Used by `rigctl sensors` to verify fan/clock control early."""
        return {}

    # --- control (M2 wires these behind the allowlist; defined now so backends are complete)
    def set_power_limit_w(self, uuid: str, watts: float) -> None:
        raise NotSupported("power limit")

    def set_locked_core_clock(self, uuid: str, min_mhz: int, max_mhz: int) -> None:
        raise NotSupported("locked core clock")

    def reset_locked_core_clock(self, uuid: str) -> None:
        raise NotSupported("reset locked core clock")

    def set_core_offset_mhz(self, uuid: str, mhz: int) -> None:
        raise NotSupported("core offset")

    def set_mem_offset_mhz(self, uuid: str, mhz: int) -> None:
        raise NotSupported("memory offset")

    def set_fan_pct(self, uuid: str, pct: int) -> None:
        raise NotSupported("fan speed")

    def set_fan_auto(self, uuid: str) -> None:
        raise NotSupported("fan auto")

    def close(self) -> None:
        pass
