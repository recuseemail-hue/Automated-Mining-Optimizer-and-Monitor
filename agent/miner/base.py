from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

UNIT_FACTORS = {"h/s": 1.0, "kh/s": 1e3, "mh/s": 1e6, "gh/s": 1e9, "th/s": 1e12}


@dataclass
class GpuMinerStats:
    index: int                      # miner's device index
    bus_id: str | None = None       # PCI bus id if the miner reports it (preferred join key)
    hashrate: float | None = None   # H/s
    accepted: int | None = None
    rejected: int | None = None
    invalid: int | None = None
    temp_c: float | None = None     # miner-reported, informational
    memj_c: float | None = None     # miner-reported memory junction temp if any
    power_w: float | None = None
    fan_pct: float | None = None
    paused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MinerStats:
    state: str                      # running|paused|stopped|unreachable|error
    coin: str | None = None
    algo: str | None = None
    pool: str | None = None
    worker: str | None = None
    uptime_s: float | None = None
    version: str | None = None
    total_hashrate: float | None = None
    accepted: int | None = None
    rejected: int | None = None
    gpus: list[GpuMinerStats] = field(default_factory=list)
    raw: Any = None
    error: str | None = None

    def by_bus(self) -> dict[str, GpuMinerStats]:
        return {g.bus_id.lower(): g for g in self.gpus if g.bus_id}

    def by_index(self) -> dict[int, GpuMinerStats]:
        return {g.index: g for g in self.gpus}


class MinerBackend:
    name = "base"

    def stats(self) -> MinerStats:
        raise NotImplementedError

    def is_running(self) -> bool:
        raise NotImplementedError

    # Process control is wired through the action allowlist in M2.
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        self.stop()
        self.start()

    def pause_gpu(self, index: int) -> None:
        raise NotImplementedError

    def resume_gpu(self, index: int) -> None:
        raise NotImplementedError
