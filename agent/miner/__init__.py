"""Miner backends: PeakMiner (real) and a simulator driven by the sim GPU state."""
from __future__ import annotations

from .base import MinerBackend, MinerStats, GpuMinerStats, UNIT_FACTORS


def make_miner(cfg, gpu_backend=None) -> MinerBackend:
    kind = cfg.get("miner.backend", "peakminer")
    if kind == "sim" or (kind == "peakminer" and getattr(gpu_backend, "name", "") == "sim"):
        from .sim import SimMiner
        return SimMiner(cfg, gpu_backend)
    if kind == "peakminer":
        from .peakminer import PeakMiner
        return PeakMiner(cfg)
    if kind == "srbminer":
        from .srbminer import SRBMiner
        return SRBMiner(cfg)
    if kind == "auto":
        from .peakminer import PeakMiner
        from .srbminer import AutoMiner, SRBMiner
        return AutoMiner(cfg, PeakMiner(cfg), SRBMiner(cfg))
    raise ValueError(f"unknown miner backend {kind!r}")


__all__ = ["MinerBackend", "MinerStats", "GpuMinerStats", "UNIT_FACTORS", "make_miner"]
