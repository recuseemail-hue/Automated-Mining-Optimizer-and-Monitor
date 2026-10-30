"""5-minute telemetry logger. No LLM, no network dependency beyond optional pool stats.

One `collect_once()` = one row per GPU + one host row at the same timestamp. The watchdog (M2)
reuses `sample_gpus()` at 15 s resolution. On a worker, rows are also queued in `outbox` for the
hub forwarder.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Any

from .config import Config
from .db import Db
from .gpu import GpuBackend, GpuInfo, GpuSample, NotSupported
from .miner import MinerBackend, MinerStats
from .platform import Platform
from .pool import PoolClient

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, cfg: Config, db: Db, gpu: GpuBackend, miner: MinerBackend, plat: Platform,
                 pool: PoolClient | None = None):
        self.cfg, self.db, self.gpu, self.miner, self.plat, self.pool = cfg, db, gpu, miner, plat, pool
        self.host_id = db.upsert_host(cfg.host_name, plat.os_string(), cfg.role,
                                      {"gpu_backend": gpu.name, "miner": miner.name})
        self._lhm = getattr(plat, "lhm", None)
        self._stop = threading.Event()
        self.last_summary: dict[str, Any] = {}

    # --- GPU registry ------------------------------------------------------------------------
    def register_gpus(self) -> list[GpuInfo]:
        infos = self.gpu.enumerate()
        for i in infos:
            self.db.upsert_gpu(self.host_id, i.uuid, i.index, i.name, i.bus_id, i.stock_pl_w, i.min_pl_w,
                               i.max_pl_w, i.vram_mb, self.cfg.gpu_control_enabled(i.uuid))
        return infos

    # --- sampling ----------------------------------------------------------------------------
    def sample_gpus(self, infos: list[GpuInfo] | None = None, mstats: MinerStats | None = None
                    ) -> list[tuple[GpuInfo, GpuSample | None]]:
        infos = infos if infos is not None else self.register_gpus()
        mstats = mstats or self.miner.stats()
        by_bus, by_idx = mstats.by_bus(), mstats.by_index()
        out: list[tuple[GpuInfo, GpuSample | None]] = []
        for n, info in enumerate(infos):
            try:
                s = self.gpu.sample(info.uuid)
            except NotSupported as e:
                log.warning("GPU %s unreadable: %s", info.uuid, e)
                out.append((info, None))
                continue
            m = (by_bus.get(info.bus_id.lower()) if info.bus_id else None) or by_idx.get(info.index)
            if m is not None:
                s.extra["miner_index"] = m.index
                s.extra["hashrate"] = m.hashrate
                s.extra["accepted"] = m.accepted
                s.extra["rejected"] = m.rejected
                s.extra["paused"] = m.paused
                if m.invalid is not None:
                    s.extra["invalid"] = m.invalid
                if s.memj_c is None and m.memj_c is not None and "miner" in self.cfg.get("gpu.memj_sources", []):
                    s.memj_c, s.extra["memj_source"] = m.memj_c, "miner"
            elif mstats.state in ("running", "paused"):
                s.extra["miner_match"] = "none"
            if s.memj_c is None and self._lhm is not None and "lhm" in self.cfg.get("gpu.memj_sources", []):
                v = self._lhm.gpu_memory_junction(info.name, n)
                if v is not None:
                    s.memj_c, s.extra["memj_source"] = v, "lhm"
            out.append((info, s))
        return out

    def collect_once(self, res: str = "5m") -> dict[str, Any]:
        ts = time.time()
        infos = self.register_gpus()
        mstats = self.miner.stats()
        rows = self.sample_gpus(infos, mstats)
        total_gpu_w = 0.0
        total_hr = 0.0
        n_ok = 0
        for info, s in rows:
            gid = self.db.gpu_id(info.uuid)
            if s is None or gid is None:
                continue
            cols = s.as_cols()
            cols["hashrate"] = s.extra.pop("hashrate", None)
            cols["accepted"] = s.extra.pop("accepted", None)
            cols["rejected"] = s.extra.pop("rejected", None)
            self.db.insert_gpu_sample(gid, ts, res, **cols)
            if self.cfg.role == "worker":
                self.db.outbox_push("gpu_sample", {"uuid": info.uuid, "ts": ts, "res": res, **_jsonable(cols)})
            total_gpu_w += s.power_w or 0.0
            total_hr += cols["hashrate"] or 0.0
            n_ok += 1

        hs = self.plat.sample()
        pool = self.pool.stats() if (self.pool and res == "5m") else None
        extra = {"uptime_s": hs.uptime_s, "miner_version": mstats.version, "miner_error": mstats.error,
                 "gpus_present": len(infos), "gpus_sampled": n_ok, "miner_uptime_s": mstats.uptime_s}
        if pool is not None:
            extra["pool_source"], extra["pool_verified"], extra["pool_ts"] = pool.source, pool.verified, pool.ts
        hcols = dict(cpu_c=hs.cpu_c, cpu_pct=hs.cpu_pct, cpu_w=hs.cpu_w, ram_used_mb=hs.ram_used_mb,
                     ram_total_mb=hs.ram_total_mb, gpu_w_total=total_gpu_w, wall_w=hs.wall_w,
                     miner_state=mstats.state, coin=mstats.coin or self.cfg.get("miner.coin"),
                     miner_hashrate=mstats.total_hashrate if mstats.total_hashrate is not None else total_hr,
                     pool_hashrate=pool.hashrate if pool else None, pool_workers=pool.workers if pool else None,
                     balance=pool.balance if pool else None, balance_usd=pool.balance_usd if pool else None,
                     extra=extra)
        self.db.insert_host_sample(self.host_id, ts, res, **hcols)
        if self.cfg.role == "worker":
            self.db.outbox_push("host_sample", {"host": self.cfg.host_name, "ts": ts, "res": res, **_jsonable(hcols)})
        self.last_summary = {"ts": ts, "gpus": n_ok, "miner": mstats.state, "hashrate": hcols["miner_hashrate"],
                             "gpu_w": total_gpu_w}
        return self.last_summary

    # --- loop --------------------------------------------------------------------------------
    def run_loop(self, interval_s: float | None = None) -> None:
        interval = float(interval_s or self.cfg.get("collector.interval_s", 300))
        prune_every = 24
        n = 0
        log.info("collector loop every %ss (role=%s, gpu=%s, miner=%s)", interval, self.cfg.role, self.gpu.name,
                 self.miner.name)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                summ = self.collect_once()
                log.info("sample ok: %s", json.dumps(summ, default=str))
            except Exception:
                log.exception("collect failed")
            n += 1
            if n % prune_every == 0:
                try:
                    self.db.prune(float(self.cfg.get("db.retain_15s_hours", 48)), float(self.cfg.get("db.retain_5m_days", 400)))
                except Exception:
                    log.exception("prune failed")
            # align to the interval grid so rows land on :00/:05/:10
            wait = interval - ((time.time()) % interval)
            self._stop.wait(max(1.0, min(wait, interval)))

    def stop(self) -> None:
        self._stop.set()


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if hasattr(v, "__dataclass_fields__"):
            v = asdict(v)
        out[k] = v
    return out
