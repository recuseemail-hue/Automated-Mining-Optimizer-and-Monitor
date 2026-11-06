"""GPU backends: real NVML and a simulator for tests and fault injection."""
from __future__ import annotations

from .base import GpuBackend, GpuInfo, GpuSample, NotSupported


def make_backend(cfg) -> GpuBackend:
    kind = cfg.get("gpu.backend", "auto")
    if kind in ("auto", "nvml"):
        try:
            from .nvml import NvmlBackend
            return NvmlBackend()
        except Exception as e:  # driver missing, no NVIDIA GPU, or pynvml not importable
            if kind == "nvml":
                raise
            import logging
            logging.getLogger(__name__).warning("NVML unavailable (%s); using simulator", e)
    from .sim import SimBackend
    return SimBackend(
        state_file=cfg.get("gpu.sim.state_file") or (cfg.data_dir / "sim_gpus.json"),
        count=int(cfg.get("gpu.sim.count", 2)),
    )


def with_control_router(reader: GpuBackend, miner, cfg) -> GpuBackend:
    """Wrap reads with the NVML->miner write router when the miner offers an OC API."""
    mode = str(cfg.get("gpu.control_backend", "auto"))
    if mode == "nvml" or not getattr(miner, "supports_oc_api", False):
        return reader
    from .router import ControlRouter
    return ControlRouter(reader, miner, mode)


__all__ = ["GpuBackend", "GpuInfo", "GpuSample", "NotSupported", "make_backend", "with_control_router"]
