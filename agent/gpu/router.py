"""Control router: reads from NVML (or the simulator), writes through NVML first and, when the
driver refuses, through the miner's overclock API (PeakMiner `/control/oc/{gpu}`), which runs
elevated and uses the same driver paths the `--gpu-*` flags use.

`gpu.control_backend`: auto (NVML then miner) | nvml | miner. The miner path needs the miner running
with `--api-control oc` and the GPU mapped by PCI bus id (fallback: index order).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from .base import GpuBackend, GpuInfo, GpuSample, NotSupported

log = logging.getLogger(__name__)


class ControlRouter(GpuBackend):
    def __init__(self, reader: GpuBackend, miner, mode: str = "auto"):
        self.reader, self.miner, self.mode = reader, miner, mode
        self.name = reader.name
        self.last_path: dict[str, str] = {}

    # --- reads pass through ---------------------------------------------------------------------
    def enumerate(self) -> list[GpuInfo]:
        return self.reader.enumerate()

    def sample(self, uuid: str) -> GpuSample:
        return self.reader.sample(uuid)

    def capabilities(self, uuid: str) -> dict[str, bool | str]:
        caps = dict(self.reader.capabilities(uuid))
        caps["control_backend"] = self.mode
        caps["miner_oc_api"] = bool(getattr(self.miner, "supports_oc_api", False))
        return caps

    def probe_write(self, uuid: str) -> dict[str, str]:
        out = self.reader.probe_write(uuid) if hasattr(self.reader, "probe_write") else {}
        if getattr(self.miner, "supports_oc_api", False):
            idx = self._miner_index(uuid)
            try:
                st = self.miner.oc_get(idx) if idx is not None else None
                out["miner_oc"] = f"available (profile source={st.get('source')})" if st else "oc level off or miner down"
            except Exception as e:
                out["miner_oc"] = str(e)[:120]
        return out

    def close(self) -> None:
        self.reader.close()

    # --- mapping ------------------------------------------------------------------------------------
    def _miner_index(self, uuid: str) -> int | None:
        info = next((i for i in self.reader.enumerate() if i.uuid == uuid), None)
        if info is None:
            return None
        try:
            ms = self.miner.stats()
        except Exception:
            return info.index
        if info.bus_id:
            g = ms.by_bus().get(_norm(info.bus_id))
            if g is not None:
                return g.index
        return info.index

    # --- writes ---------------------------------------------------------------------------------------
    def _write(self, uuid: str, label: str, nvml_fn: Callable[[], None], miner_knobs: dict[str, Any] | None) -> None:
        can_miner = bool(getattr(self.miner, "supports_oc_api", False)) and miner_knobs is not None
        if self.mode in ("auto", "nvml"):
            try:
                nvml_fn()
                self.last_path[f"{uuid}:{label}"] = "nvml"
                return
            except NotSupported as e:
                if self.mode == "nvml" or not can_miner:
                    raise
                log.info("%s via NVML refused (%s); trying miner OC API", label, e)
        if not can_miner:
            raise NotSupported(f"{label}: no control path (mode={self.mode})")
        idx = self._miner_index(uuid)
        if idx is None:
            raise NotSupported(f"{label}: GPU {uuid} not mapped to a miner device")
        try:
            resp = self.miner.oc_set(idx, **miner_knobs)
        except NotImplementedError as e:
            raise NotSupported(str(e)) from e
        except Exception as e:
            raise NotSupported(f"{label} via miner: {e}") from e
        skipped = [s for s in (resp.get("skipped") or []) if any(k in str(s) for k in miner_knobs)]
        if skipped:
            raise NotSupported(f"{label}: miner/driver refused: {skipped}")
        self.last_path[f"{uuid}:{label}"] = "miner"

    def set_power_limit_w(self, uuid: str, watts: float) -> None:
        self._write(uuid, "power_limit", lambda: self.reader.set_power_limit_w(uuid, watts), {"power": int(round(watts))})

    def set_locked_core_clock(self, uuid: str, min_mhz: int, max_mhz: int) -> None:
        self._write(uuid, "core_lock", lambda: self.reader.set_locked_core_clock(uuid, min_mhz, max_mhz), {"lcore": int(max_mhz)})

    def reset_locked_core_clock(self, uuid: str) -> None:
        self._write(uuid, "core_lock_reset", lambda: self.reader.reset_locked_core_clock(uuid), {"lcore": None})

    def set_locked_mem_clock(self, uuid: str, min_mhz: int, max_mhz: int) -> None:
        self._write(uuid, "mem_lock", lambda: self.reader.set_locked_mem_clock(uuid, min_mhz, max_mhz), {"lmem": int(max_mhz)})

    def reset_locked_mem_clock(self, uuid: str) -> None:
        self._write(uuid, "mem_lock_reset", lambda: self.reader.reset_locked_mem_clock(uuid), {"lmem": None})

    def set_core_offset_mhz(self, uuid: str, mhz: int) -> None:
        self._write(uuid, "core_offset", lambda: self.reader.set_core_offset_mhz(uuid, mhz), {"core": int(mhz)})

    def set_mem_offset_mhz(self, uuid: str, mhz: int) -> None:
        self._write(uuid, "mem_offset", lambda: self.reader.set_mem_offset_mhz(uuid, mhz), {"mem": int(mhz)})

    def set_fan_pct(self, uuid: str, pct: int) -> None:
        self._write(uuid, "fan", lambda: self.reader.set_fan_pct(uuid, pct), {"fan": {"duty": int(pct)}})

    def set_fan_auto(self, uuid: str) -> None:
        self._write(uuid, "fan_auto", lambda: self.reader.set_fan_auto(uuid), {"fan": None})


def _norm(bus: str | None) -> str:
    from ..miner.base import norm_bus
    return norm_bus(bus) or ""
