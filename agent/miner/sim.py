"""Simulated miner. Hashrate and shares follow the sim GPU model; faults can zero a GPU."""
from __future__ import annotations

import json
import time

from .base import GpuMinerStats, MinerBackend, MinerStats


class SimMiner(MinerBackend):
    name = "sim"

    def __init__(self, cfg, gpu_backend):
        self.cfg = cfg
        self.gpu = gpu_backend
        self._state_path = self.gpu.path.with_name("sim_miner.json")
        if not self._state_path.is_file():
            self._save({"running": True, "started": time.time(), "paused": [], "shares": {}})

    def _load(self) -> dict:
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _save(self, st: dict) -> None:
        self._state_path.write_text(json.dumps(st), encoding="utf-8")

    def is_running(self) -> bool:
        return bool(self._load().get("running"))

    def start(self) -> None:
        st = self._load()
        st.update(running=True, started=time.time())
        self._save(st)

    def stop(self) -> None:
        st = self._load()
        st["running"] = False
        self._save(st)

    def pause_gpu(self, index: int) -> None:
        st = self._load()
        if index not in st["paused"]:
            st["paused"].append(index)
        self._save(st)

    def resume_gpu(self, index: int) -> None:
        st = self._load()
        st["paused"] = [i for i in st["paused"] if i != index]
        self._save(st)

    def stats(self) -> MinerStats:
        st = self._load()
        if not st.get("running"):
            return MinerStats(state="stopped", coin=self.cfg.get("miner.coin"))
        gpus = []
        total = 0.0
        for info in self.gpu.enumerate():
            g = self.gpu._gpu(info.uuid)
            m = self.gpu.model(g)
            f = g.get("faults", {})
            paused = info.index in st["paused"]
            hr = 0.0 if paused else f.get("hashrate", m["hashrate"])
            sh = st["shares"].setdefault(str(info.index), {"acc": 0, "rej": 0})
            # ~1 share / 30 s at full hashrate, split by rejection rate
            new = (hr / m["hashrate"]) * 10 if m["hashrate"] else 0
            sh["acc"] += int(new * (1 - m["rej_rate"]))
            sh["rej"] += int(round(new * m["rej_rate"]))
            gpus.append(GpuMinerStats(index=info.index, bus_id=info.bus_id, hashrate=hr, accepted=sh["acc"],
                                      rejected=sh["rej"], temp_c=m["core_c"], memj_c=m["memj_c"],
                                      power_w=m["power_w"], paused=paused))
            total += hr
        self._save(st)
        return MinerStats(state="paused" if gpus and all(g.paused for g in gpus) else "running",
                          coin=self.cfg.get("miner.coin"), algo=self.cfg.get("miner.algo"),
                          pool=self.cfg.get("miner.pool"), worker="sim", uptime_s=time.time() - st["started"],
                          version="sim", total_hashrate=total, accepted=sum(g.accepted for g in gpus),
                          rejected=sum(g.rejected for g in gpus), gpus=gpus)
