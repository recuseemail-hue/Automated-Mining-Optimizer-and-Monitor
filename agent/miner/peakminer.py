"""PeakMiner backend.

Stats come from GET <api_url> (default http://127.0.0.1:4068/summary). The exact JSON layout is
NOT yet verified against a real instance (build sandbox could not reach the wiki); the parser is
tolerant and reads several plausible key names. Capture the real response with
`rigctl miner dump` on the rig and update tests/fixtures/peakminer_summary.json, then tighten.

Hashrate unit defaults to kH/s per the PeakMiner wiki (`miner.api_hashrate_unit`).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from .base import UNIT_FACTORS, GpuMinerStats, MinerBackend, MinerStats

log = logging.getLogger(__name__)


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def parse_summary(raw: Any, unit: str = "kH/s", coin: str | None = None) -> MinerStats:
    """Parse a PeakMiner /summary payload into MinerStats. Pure function; unit-tested on fixtures."""
    factor = UNIT_FACTORS.get(unit.lower(), 1.0)
    if not isinstance(raw, dict):
        return MinerStats(state="error", raw=raw, error="summary is not a JSON object")

    devices = _first(raw, "gpus", "devices", "workers", "threads", default=[]) or []
    if isinstance(devices, dict):          # {"0": {...}, "1": {...}}
        devices = [dict(v, index=int(k)) if isinstance(v, dict) and str(k).isdigit() else v
                   for k, v in devices.items()]

    gpus: list[GpuMinerStats] = []
    for i, d in enumerate(devices):
        if not isinstance(d, dict):
            continue
        hr = _num(_first(d, "hashrate", "hash_rate", "hr", "speed", "hashrate_1m"))
        gpus.append(GpuMinerStats(
            index=_int(_first(d, "index", "id", "gpu_id", "device_id", default=i)) or i,
            bus_id=_first(d, "bus_id", "pci_bus_id", "pci", "busid"),
            hashrate=None if hr is None else hr * factor,
            accepted=_int(_first(d, "accepted", "accepted_shares", "shares_accepted", "valid")),
            rejected=_int(_first(d, "rejected", "rejected_shares", "shares_rejected")),
            invalid=_int(_first(d, "invalid", "invalid_shares", "stale")),
            temp_c=_num(_first(d, "temperature", "temp", "core_temp", "gpu_temp")),
            memj_c=_num(_first(d, "memory_temperature", "mem_temp", "memory_temp", "memj", "vram_temp")),
            power_w=_num(_first(d, "power", "power_w", "power_usage")),
            fan_pct=_num(_first(d, "fan", "fan_speed", "fan_pct")),
            paused=bool(_first(d, "paused", default=False)) or str(_first(d, "status", "state", default="")).lower() == "paused",
            extra={k: v for k, v in d.items() if k in ("name", "status", "state", "lhr", "errors")},
        ))

    total = _num(_first(raw, "total_hashrate", "hashrate", "hashrate_total", "total"))
    if total is None and gpus:
        total = sum(g.hashrate or 0.0 for g in gpus)
    elif total is not None:
        total *= factor

    acc = _int(_first(raw, "accepted", "accepted_shares", "shares_accepted"))
    rej = _int(_first(raw, "rejected", "rejected_shares", "shares_rejected"))
    if acc is None and gpus:
        acc = sum(g.accepted or 0 for g in gpus)
    if rej is None and gpus:
        rej = sum(g.rejected or 0 for g in gpus)

    state = "running"
    if gpus and all(g.paused for g in gpus):
        state = "paused"
    return MinerStats(
        state=state,
        coin=_first(raw, "coin", "currency", default=coin),
        algo=_first(raw, "algo", "algorithm"),
        pool=_first(raw, "pool", "pool_url", "server"),
        worker=_first(raw, "worker", "worker_name", "rig"),
        uptime_s=_num(_first(raw, "uptime", "uptime_s", "uptime_seconds")),
        version=_first(raw, "version", "miner_version"),
        total_hashrate=total, accepted=acc, rejected=rej, gpus=gpus, raw=raw,
    )


class PeakMiner(MinerBackend):
    name = "peakminer"

    def __init__(self, cfg):
        self.cfg = cfg
        self.api_url = cfg.get("miner.api_url")
        self.timeout = float(cfg.get("miner.api_timeout_s", 3))
        self.unit = cfg.get("miner.api_hashrate_unit", "kH/s")
        self.exe = Path(cfg.get("miner.exe", ""))
        self._proc: subprocess.Popen | None = None

    def fetch_raw(self) -> Any:
        with urllib.request.urlopen(self.api_url, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def stats(self) -> MinerStats:
        try:
            raw = self.fetch_raw()
        except Exception as e:
            return MinerStats(state="unreachable", coin=self.cfg.get("miner.coin"), error=str(e)[:200])
        return parse_summary(raw, self.unit, self.cfg.get("miner.coin"))

    def is_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        return self.stats().state in ("running", "paused")

    # --- process control (used by the M2 action layer; never called by the collector) --------
    def command(self) -> list[str]:
        from ..secrets import wallet_for
        coin = self.cfg.get("miner.coin", "PRL")
        wallet = wallet_for(coin) or "${WALLET_MISSING}"
        worker = self.cfg.get("miner.worker") or os.environ.get("COMPUTERNAME") or "rig"
        port = self.api_url.split(":")[-1].split("/")[0]
        # Flag names follow the PeakMiner wiki; verified on the rig in M2.
        cmd = [str(self.exe), "--algo", self.cfg.get("miner.algo", "pearl"),
               "--pool", self.cfg.get("miner.pool"), "--wallet", wallet, "--worker", worker,
               "--api-port", port]
        cmd += list(self.cfg.get("miner.extra_args", []) or [])
        return cmd

    def start(self) -> None:
        if self.is_running():
            return
        if not self.exe.is_file():
            raise FileNotFoundError(f"miner exe not found: {self.exe}")
        logs = self.cfg.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / "peakminer.log", "ab")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(self.command(), stdout=out, stderr=subprocess.STDOUT,
                                      cwd=str(self.exe.parent), creationflags=flags)
        log.info("peakminer started pid=%s", self._proc.pid)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        else:
            # Not our child (e.g. after a service restart): kill by image name.
            import psutil
            for p in psutil.process_iter(["name"]):
                if (p.info["name"] or "").lower().startswith(self.exe.stem.lower()):
                    p.terminate()
        self._proc = None
        time.sleep(1)

    def pause_gpu(self, index: int) -> None:
        # PeakMiner exposes temperature pause/resume via flags, not a documented runtime API;
        # until verified, per-GPU pause is implemented by the action layer as a restart with the
        # device excluded. See docs/sensors.md.
        raise NotImplementedError("per-GPU pause via API not verified for PeakMiner")

    def resume_gpu(self, index: int) -> None:
        raise NotImplementedError("per-GPU resume via API not verified for PeakMiner")
