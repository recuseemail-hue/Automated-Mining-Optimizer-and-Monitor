"""SRBMiner-MULTI, read-only. Used when another launcher (e.g. the unMineable app) runs the miner:
rig-overseer reads its HTTP stats API and never starts/stops it (miner.managed must be false).

The API port is read from the running process's command line (`--api-port N`; unMiner picks a random
port per launch), falling back to miner.srbminer.api_port. The JSON shape below is from SRBMiner's
documented API (UNVERIFIED here); `rigctl miner dump` prints the raw response so it can be corrected:
  {"miner_version", "mining_time", "rig_name",
   "algorithms": [{"name": "pearl", "hashrate": {"gpu": {"total": H/s, "gpu0": H/s}}, "shares": {"accepted", "rejected"}, "pool": {"pool": url}}],
   "gpu_devices": [{"id": 0, "bus_id": 5, "model": "...", "temperature", "fan_speed", "core_clock", "memory_clock"}]}
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Any

from .base import GpuMinerStats, MinerBackend, MinerStats

log = logging.getLogger(__name__)
ALGO_TO_COIN = {"pearl": "PRL", "quantus": "QTC"}


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_stats(raw: Any, coin_hint: str | None = None) -> MinerStats:
    if not isinstance(raw, dict):
        return MinerStats(state="error", raw=raw, error="stats is not a JSON object")
    algos = raw.get("algorithms") or []
    algo = algos[0] if algos and isinstance(algos[0], dict) else {}
    hr = (algo.get("hashrate") or {}).get("gpu") or {}
    shares = algo.get("shares") or {}
    pool = algo.get("pool") or {}
    gpus: list[GpuMinerStats] = []
    for d in raw.get("gpu_devices") or []:
        if not isinstance(d, dict):
            continue
        idx = int(d.get("id", len(gpus)))
        per = _num(hr.get(f"gpu{idx}")) if isinstance(hr, dict) else None
        if per is None:
            per = _num(d.get("hashrate"))
        bus = d.get("bus_id")
        bus_s = f"{int(bus):02x}:00.0" if isinstance(bus, (int, float)) else (str(bus) if bus else None)
        gpus.append(GpuMinerStats(index=idx, bus_id=bus_s, hashrate=per, accepted=None, rejected=None,
                                  temp_c=_num(d.get("temperature")), fan_pct=_num(d.get("fan_speed")),
                                  power_w=_num(d.get("power")), paused=bool(d.get("paused", False)),
                                  extra={k: d[k] for k in ("model", "core_clock", "memory_clock") if k in d}))
    total = _num(hr.get("total")) if isinstance(hr, dict) else None
    if total is None and gpus:
        total = sum(g.hashrate or 0.0 for g in gpus)
    acc, rej = _num(shares.get("accepted")), _num(shares.get("rejected"))
    if len(gpus) == 1 and acc is not None:               # single card: pool shares are its shares
        gpus[0].accepted, gpus[0].rejected = int(acc), int(rej or 0)
    name = str(algo.get("name") or "")
    st = MinerStats(state="running" if total else "running", coin=ALGO_TO_COIN.get(name.lower(), coin_hint), algo=name or None,
                    pool=pool.get("pool") or pool.get("url"), worker=raw.get("rig_name"), uptime_s=_num(raw.get("mining_time")),
                    version=raw.get("miner_version"), total_hashrate=total,
                    accepted=int(acc) if acc is not None else None, rejected=int(rej) if rej is not None else None,
                    gpus=gpus, raw=raw)
    st.extra = {"source": "srbminer", "read_only": True}
    return st


class SRBMiner(MinerBackend):
    name = "srbminer"
    supports_oc_api = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout = float(cfg.get("miner.api_timeout_s", 3))
        self._port: int | None = None
        self.coin_override: str | None = None

    # --- discovery ----------------------------------------------------------------------------------
    def process(self):
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            if (p.info["name"] or "").lower().startswith("srbminer"):
                return p
        return None

    def detect_port(self) -> int | None:
        p = self.process()
        if p is None:
            self._port = None
            return None
        try:
            cmd = p.info.get("cmdline") or p.cmdline()
        except Exception:
            cmd = []
        for i, a in enumerate(cmd):
            if a == "--api-port" and i + 1 < len(cmd) and str(cmd[i + 1]).isdigit():
                self._port = int(cmd[i + 1])
                return self._port
            m = re.match(r"--api-port=(\d+)", str(a))
            if m:
                self._port = int(m.group(1))
                return self._port
        self._port = int(self.cfg.get("miner.srbminer.api_port", 21550))
        return self._port

    def port(self) -> int | None:
        return self._port or self.detect_port()

    def fetch_raw(self) -> Any:
        port = self.port()
        if not port:
            raise RuntimeError("SRBMiner-MULTI is not running")
        for path in ("/", "/stats"):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as e:                       # port changes per launch: re-detect once
                last = e
                self._port = None
                if not self.port():
                    break
        raise RuntimeError(f"SRBMiner API unreachable on port {port}: {last}")

    # --- MinerBackend -----------------------------------------------------------------------------------
    def active_coin(self) -> dict:
        coin = self.coin_override or self.cfg.get("miner.coin", "PRL")
        cat = (self.cfg.get("coins", {}) or {}).get(coin, {})
        return {"coin": coin, "algo": cat.get("algo", "pearl"), "pool": cat.get("pool"), "dev_fee_pct": cat.get("dev_fee_pct", 0.85)}

    def stats(self) -> MinerStats:
        if self.process() is None:
            return MinerStats(state="stopped", coin=self.active_coin()["coin"], error="SRBMiner-MULTI not running")
        try:
            raw = self.fetch_raw()
        except Exception as e:
            return MinerStats(state="unreachable", coin=self.active_coin()["coin"], error=str(e)[:200])
        return parse_stats(raw, self.active_coin()["coin"])

    def is_running(self) -> bool:
        return self.process() is not None

    def start(self) -> None:
        raise NotImplementedError("srbminer backend is read-only (started by the unMineable app)")

    def stop(self) -> None:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower().startswith("srbminer"):
                p.terminate()

    def pause_gpu(self, index: int) -> None:
        raise NotImplementedError("srbminer backend has no pause API")

    def resume_gpu(self, index: int) -> None:
        raise NotImplementedError("srbminer backend has no resume API")


class AutoMiner(MinerBackend):
    """Reads whichever miner is actually running: PeakMiner (managed, full control) when its API answers,
    else SRBMiner (read-only). Control calls go to PeakMiner only."""
    name = "auto"

    def __init__(self, cfg, peak, srb):
        self.cfg, self.peak, self.srb = cfg, peak, srb
        self.current: MinerBackend = peak

    @property
    def supports_oc_api(self) -> bool:
        return self.current is self.peak and getattr(self.peak, "supports_oc_api", False)

    @property
    def coin_override(self):
        return self.peak.coin_override

    @coin_override.setter
    def coin_override(self, v):
        self.peak.coin_override = v
        self.srb.coin_override = v

    def active_coin(self) -> dict:
        return self.current.active_coin()

    def stats(self) -> MinerStats:
        st = self.peak.stats()
        if st.state in ("running", "paused", "unauthorized"):
            self.current = self.peak
            return st
        if self.srb.process() is not None:
            self.current = self.srb
            return self.srb.stats()
        self.current = self.peak
        return st

    def is_running(self) -> bool:
        return self.peak.is_running() or self.srb.is_running()

    def start(self) -> None:
        if self.srb.process() is not None:
            raise RuntimeError("SRBMiner (unMineable app) is running; close it before starting PeakMiner")
        self.peak.start()

    def stop(self) -> None:
        self.peak.stop()
        if self.srb.process() is not None:
            self.srb.stop()

    def restart(self) -> None:
        self.peak.restart()

    def fetch_raw(self):
        self.stats()                                       # refresh which miner is live
        return self.current.fetch_raw()

    def pause_gpu(self, index: int) -> None:
        self.current.pause_gpu(index)

    def resume_gpu(self, index: int) -> None:
        self.current.resume_gpu(index)

    def __getattr__(self, item):                           # oc_get/oc_set/api_key/command/... -> PeakMiner
        return getattr(self.peak, item)
