"""Simulated GPUs.

State lives in a JSON file so tests, `rigctl sim`, and the watchdog tests can inject faults
(overheat, fan stop, GPU drop) without touching hardware. The thermal/power model is crude but
monotonic: more power limit -> more hashrate, more heat; memory offset -> more hashrate until it
starts producing rejected shares.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .base import GpuBackend, GpuInfo, GpuSample, NotSupported

DEFAULT_GPU = {
    "name": "NVIDIA GeForce RTX 3080 (sim)",
    "stock_pl_w": 340.0, "min_pl_w": 100.0, "max_pl_w": 370.0, "vram_mb": 10240,
    "pl_w": 240.0, "core_offset_mhz": 0, "mem_offset_mhz": 0, "locked_core": None,
    "fan_pct": None,                      # None -> auto
    "ambient_c": 30.0,
    "faults": {},                         # e.g. {"core_c": 92, "fan_rpm": 0, "missing": true, "memj_c": 108}
}


class SimBackend(GpuBackend):
    name = "sim"

    def __init__(self, state_file: Path | str, count: int = 2, seed: int | None = None):
        self.path = Path(state_file)
        self.rng = random.Random(seed)
        if not self.path.is_file():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            gpus = {}
            for i in range(count):
                uuid = f"GPU-SIM-{i:04d}"
                gpus[uuid] = {**json.loads(json.dumps(DEFAULT_GPU)), "index": i, "bus_id": f"00000000:0{i+1}:00.0"}
            self._write({"gpus": gpus})

    # --- state file ---------------------------------------------------------------
    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, st: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _gpu(self, uuid: str) -> dict:
        g = self._read()["gpus"].get(uuid)
        if g is None or g.get("faults", {}).get("missing"):
            raise NotSupported(f"GPU {uuid} not present")
        return g

    def _update(self, uuid: str, **kv) -> None:
        st = self._read()
        st["gpus"][uuid].update(kv)
        self._write(st)

    def inject(self, uuid: str, **faults) -> None:
        """Set fault overrides; pass value None to clear a fault, or faults={} to clear all."""
        st = self._read()
        cur = st["gpus"][uuid].setdefault("faults", {})
        for k, v in faults.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        self._write(st)

    def clear_faults(self, uuid: str) -> None:
        st = self._read()
        st["gpus"][uuid]["faults"] = {}
        self._write(st)

    # --- model ----------------------------------------------------------------------
    def model(self, g: dict) -> dict:
        """Deterministic expected values for a GPU state (no noise). Used by the sim miner too."""
        pl = g["pl_w"]
        eff_pl = min(pl, g["stock_pl_w"])
        power = eff_pl * 0.97
        # hashrate (H/s) ~ 100 TH/s at 240 W for a 3080-on-Pearl stand-in; mem offset helps until ~+1000
        mem = g["mem_offset_mhz"]
        core_bonus = (g["core_offset_mhz"] or 0) / 3000.0
        mem_bonus = min(mem, 1000) / 10000.0
        base = 100e12 * (eff_pl / 240.0) ** 0.6
        hashrate = base * (1 + core_bonus + mem_bonus)
        rej_rate = 0.0 if mem <= 1000 else min(0.5, (mem - 1000) / 400.0)   # GDDR6X: errors, not crashes
        if mem > 1000:
            hashrate *= 1 - rej_rate                                       # error correction eats hashrate
        core_c = g["ambient_c"] + power * 0.17 - (0 if g["fan_pct"] is None else (g["fan_pct"] - 60) * 0.15)
        memj_c = core_c + 18 + max(0, mem) / 100.0
        fan_pct = g["fan_pct"] if g["fan_pct"] is not None else min(100, max(35, 35 + (core_c - 50) * 2.5))
        core_mhz = g["locked_core"][1] if g["locked_core"] else 1400 + (eff_pl - 240) * 1.2 + (g["core_offset_mhz"] or 0)
        return {
            "power_w": power, "core_c": core_c, "memj_c": memj_c, "fan_pct": fan_pct,
            "fan_rpm": fan_pct * 30, "core_mhz": core_mhz, "mem_mhz": 9501 + mem / 2.0,
            "hashrate": hashrate, "rej_rate": rej_rate,
        }

    # --- backend API ------------------------------------------------------------------
    def enumerate(self) -> list[GpuInfo]:
        out = []
        for uuid, g in self._read()["gpus"].items():
            if g.get("faults", {}).get("missing"):
                continue
            out.append(GpuInfo(uuid, g["index"], g["name"], g["bus_id"], g["stock_pl_w"], g["min_pl_w"],
                               g["max_pl_w"], g["vram_mb"]))
        return out

    def sample(self, uuid: str) -> GpuSample:
        g = self._gpu(uuid)
        m = self.model(g)
        f = g.get("faults", {})
        n = lambda v, sd: v + self.rng.gauss(0, sd)  # noqa: E731
        s = GpuSample(
            ts=time.time(),
            power_w=f.get("power_w", n(m["power_w"], 2)),
            core_c=f.get("core_c", n(m["core_c"], 0.5)),
            memj_c=f.get("memj_c", n(m["memj_c"], 0.7)),
            fan_pct=f.get("fan_pct", m["fan_pct"]),
            fan_rpm=f.get("fan_rpm", n(m["fan_rpm"], 20)),
            core_mhz=f.get("core_mhz", n(m["core_mhz"], 5)),
            mem_mhz=m["mem_mhz"],
            pl_w=g["pl_w"],
            util_pct=f.get("util_pct", 100.0),
            vram_used_mb=4200.0,
            core_offset_mhz=g["core_offset_mhz"],
            mem_offset_mhz=g["mem_offset_mhz"],
            throttle=list(f.get("throttle", [])) + (["hw_thermal"] if m["core_c"] > 83 else []),
            extra={"sim": True},
        )
        return s

    def capabilities(self, uuid: str) -> dict[str, bool | str]:
        self._gpu(uuid)
        return {"driver": "sim", "power_limit": True, "core_offset": True, "mem_offset": True,
                "locked_clocks": True, "fan": True, "memj": True}

    def set_power_limit_w(self, uuid: str, watts: float) -> None:
        g = self._gpu(uuid)
        if not g["min_pl_w"] <= watts <= g["max_pl_w"]:
            raise NotSupported(f"power limit {watts} outside driver range")
        self._update(uuid, pl_w=float(watts))

    def set_locked_core_clock(self, uuid: str, min_mhz: int, max_mhz: int) -> None:
        self._gpu(uuid)
        self._update(uuid, locked_core=[int(min_mhz), int(max_mhz)])

    def reset_locked_core_clock(self, uuid: str) -> None:
        self._gpu(uuid)
        self._update(uuid, locked_core=None)

    def set_core_offset_mhz(self, uuid: str, mhz: int) -> None:
        self._gpu(uuid)
        self._update(uuid, core_offset_mhz=int(mhz))

    def set_mem_offset_mhz(self, uuid: str, mhz: int) -> None:
        self._gpu(uuid)
        self._update(uuid, mem_offset_mhz=int(mhz))

    def set_fan_pct(self, uuid: str, pct: int) -> None:
        self._gpu(uuid)
        self._update(uuid, fan_pct=int(pct))

    def set_fan_auto(self, uuid: str) -> None:
        self._gpu(uuid)
        self._update(uuid, fan_pct=None)
