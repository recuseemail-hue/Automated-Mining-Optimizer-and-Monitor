"""PeakMiner backend, aligned with the official docs for v2.20.5 (README.md / API.md in
github.com/peakminer/peakminer): `GET /summary` reports hashrate in H/s, per-GPU
`accepted_shares` / `invalid_shares`, `pci_bus_id` like "06:00.0", and `status: "paused"`.
With `--api-key` + `--api-control pause,oc` the miner also accepts per-GPU pause/resume and
in-place overclock writes (`/control/oc/{gpu}`), which rig-overseer uses as a second control
path when NVML refuses a write (Windows GeForce fan control, for example).

Still to confirm on the rig: the exact text of refusals in `skipped`, and whether the miner's
NVML/NVAPI writes succeed where ours do not. `rigctl miner dump` prints the raw summary.
"""
from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..secrets import get_secret, set_secret_store
from .base import UNIT_FACTORS, GpuMinerStats, MinerBackend, MinerStats, norm_bus

log = logging.getLogger(__name__)


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def parse_summary(raw: Any, unit: str = "H/s", coin: str | None = None) -> MinerStats:
    """Parse `GET /summary` (fields are only ever added by PeakMiner, so unknown keys are ignored)."""
    factor = UNIT_FACTORS.get(unit.lower(), 1.0)
    if not isinstance(raw, dict):
        return MinerStats(state="error", raw=raw, error="summary is not a JSON object")
    gpus: list[GpuMinerStats] = []
    for i, d in enumerate(raw.get("gpus") or []):
        if not isinstance(d, dict):
            continue
        hr = _num(_first(d, "hashrate"))
        status = str(d.get("status") or "").lower()
        gpus.append(GpuMinerStats(
            index=_int(_first(d, "id", "index", default=i)) if _first(d, "id", "index") is not None else i,
            bus_id=norm_bus(_first(d, "pci_bus_id", "bus_id")),
            hashrate=None if hr is None else hr * factor,
            accepted=_int(_first(d, "accepted_shares", "accepted")),
            rejected=_int(_first(d, "invalid_shares", "rejected_shares", "rejected")),
            invalid=_int(_first(d, "invalid_shares")),
            temp_c=_num(_first(d, "temperature_c", "temperature")),
            memj_c=_num(_first(d, "memory_temperature_c", "mem_temp")),      # not emitted today; kept in case it is added
            power_w=_num(_first(d, "power_w", "power")),
            fan_pct=_num(_first(d, "fan_pct", "fan")),
            paused=(status == "paused"),
            extra={k: d[k] for k in ("name", "utilization_pct", "core_clock_mhz", "mem_clock_mhz", "hashrate_per_watt", "status")
                   if k in d},
        ))
    total = _num(_first(raw, "hashrate", "total_hashrate"))
    if total is None and gpus:
        total = sum(g.hashrate or 0.0 for g in gpus)
    elif total is not None:
        total *= factor
    pool = raw.get("pool") if isinstance(raw.get("pool"), dict) else {}
    state = "running"
    if gpus and all(g.paused for g in gpus):
        state = "paused"
    if pool and pool.get("connected") is False:
        state = "running"            # mining process is up; pool disconnect shows in extra
    st = MinerStats(
        state=state, coin=coin, algo=raw.get("algo"), pool=pool.get("url"), worker=pool.get("worker"),
        uptime_s=_num(raw.get("uptime")), version=raw.get("version"), total_hashrate=total,
        accepted=_int(_first(raw, "accepted_shares", "accepted")) if _first(raw, "accepted_shares", "accepted") is not None
        else (sum(g.accepted or 0 for g in gpus) if gpus else None),
        rejected=_int(_first(raw, "invalid_shares", "rejected_shares")) if _first(raw, "invalid_shares", "rejected_shares") is not None
        else (sum(g.rejected or 0 for g in gpus) if gpus else None),
        gpus=gpus, raw=raw,
    )
    st.extra = {"dev_fee_percent": raw.get("dev_fee_percent"), "power_w": raw.get("power_w"),
                "pool_connected": pool.get("connected"), "pool_ping_ms": pool.get("ping_ms"),
                "efficiency_pct": raw.get("efficiency_pct"), "last_share_at": raw.get("last_share_at")}
    return st


class PeakMiner(MinerBackend):
    name = "peakminer"
    supports_oc_api = True

    def __init__(self, cfg):
        self.cfg = cfg
        port = int(cfg.get("miner.api_port", 4068))
        self.base = f"http://127.0.0.1:{port}"
        self.timeout = float(cfg.get("miner.api_timeout_s", 3))
        self.unit = cfg.get("miner.api_hashrate_unit", "H/s")
        self.exe = Path(cfg.get("miner.exe", ""))
        self.control = [str(x) for x in (cfg.get("miner.api_control", ["pause", "oc"]) or [])]
        self._proc: subprocess.Popen | None = None
        self.coin_override: str | None = None

    # --- HTTP -----------------------------------------------------------------------------------
    def _headers(self, write: bool = False) -> dict[str, str]:
        h = {"Accept": "application/json"}
        key = get_secret("PEAKMINER_API_KEY")
        if key:
            h["X-API-Key"] = key
        if write:
            h["Content-Type"] = "application/json"
        return h

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers(body is not None))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                txt = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(txt) if txt.strip() else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(txt)
            except ValueError:
                return e.code, {"error": txt[:200]}

    def fetch_raw(self) -> Any:
        code, body = self._req("GET", "/summary")
        if code != 200:
            raise RuntimeError(f"/summary {code}: {body}")
        return body

    def health(self) -> dict | None:
        try:
            code, body = self._req("GET", "/health", timeout=2)
            return body if code == 200 else None
        except Exception:
            return None

    # --- stats ----------------------------------------------------------------------------------
    def active_coin(self) -> dict:
        coin = self.coin_override or self.cfg.get("miner.coin", "PRL")
        cat = (self.cfg.get("coins", {}) or {}).get(coin, {})
        return {"coin": coin, "algo": cat.get("algo", self.cfg.get("miner.algo", "pearl")),
                "pool": cat.get("pool"), "dev_fee_pct": cat.get("dev_fee_pct", 2.0)}

    def stats(self) -> MinerStats:
        coin = self.active_coin()["coin"]
        try:
            code, body = self._req("GET", "/summary")
        except Exception as e:
            return MinerStats(state="unreachable", coin=coin, error=str(e)[:200])
        if code == 401:
            # the miner is up but our key does not match (or is unreadable): report it, never treat as down
            return MinerStats(state="unauthorized", coin=coin, error="miner API key rejected: PEAKMINER_API_KEY differs from the "
                                                                      "running miner's key (restart the miner from rigctl to re-sync)")
        if code != 200:
            return MinerStats(state="error", coin=coin, error=f"/summary {code}: {body}")
        raw = body
        st = parse_summary(raw, self.unit, coin)
        if st.state == "running" and not st.gpus:
            st.state = "error"
            st.error = "miner up but reports no GPUs"
        return st

    def is_running(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        return self.health() is not None

    # --- process ----------------------------------------------------------------------------------
    def api_key(self) -> str:
        """The miner refuses to start with control levels and no key, so one is generated on first use."""
        key = get_secret("PEAKMINER_API_KEY")
        if not key:
            key = pysecrets.token_urlsafe(24)
            where = set_secret_store("PEAKMINER_API_KEY", key)
            os.environ["PEAKMINER_API_KEY"] = key
            log.info("generated PEAKMINER_API_KEY (stored in %s)", where)
        return key

    def worker_name(self) -> str:
        worker = self.cfg.get("miner.worker") or os.environ.get("COMPUTERNAME") or self.cfg.host_name
        return "".join(ch for ch in str(worker) if ch.isalnum() or ch in "-_")[:32] or "rig"

    def pool_target(self):
        from ..pool.resolve import resolve_pool
        return resolve_pool(self.cfg, self.active_coin()["coin"], self.worker_name())

    def command(self) -> list[str]:
        ac = self.active_coin()
        pt = self.pool_target()
        if not pt.url:
            raise RuntimeError(pt.note or f"no pool URL for {ac['coin']}")
        if not pt.login:
            raise RuntimeError(f"refusing to start the miner without a payout address: secret {pt.wallet_secret} is not readable "
                               f"by this process (services run as SYSTEM: run `rigctl secret migrate`)")
        login = pt.login
        port = int(self.cfg.get("miner.api_port", 4068))
        logs = self.cfg.data_dir / "logs"
        state = self.cfg.data_dir / "peakminer"
        cmd = [str(self.exe), "--coin", ac["algo"], "-o", str(pt.url), "-u", login,
               "-a", f"127.0.0.1:{port}", "--state-dir", str(state),
               "--log-file", str(logs / "peakminer.log"), "--log-append", "--no-color", "--no-tips"]
        if self.control:
            cmd += ["--api-key", self.api_key(), "--api-control", ",".join(self.control)]
        stop = self.cfg.get("miner.temp_stop_c")
        if stop:
            cmd += ["--gpu-temp-stop", str(int(stop))]
            if self.cfg.get("miner.temp_start_c"):
                cmd += ["--gpu-temp-start", str(int(self.cfg.get("miner.temp_start_c")))]
        cmd += [str(x) for x in (self.cfg.get("miner.extra_args", []) or [])]
        return cmd

    def start(self) -> None:
        if self.is_running():
            return
        if not self.exe.is_file():
            raise FileNotFoundError(f"miner exe not found: {self.exe}")
        (self.cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.cfg.data_dir / "peakminer").mkdir(parents=True, exist_ok=True)
        out = open(self.cfg.data_dir / "logs" / "peakminer.stdout.log", "ab")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        self._proc = subprocess.Popen(self.command(), stdout=out, stderr=subprocess.STDOUT, cwd=str(self.exe.parent),
                                      creationflags=flags, env=env)
        log.info("peakminer started pid=%s", self._proc.pid)
        for _ in range(30):                    # wait up to 30 s for the API
            time.sleep(1)
            if self.health():
                return

    def restart(self) -> None:
        self.command()               # raises before anything is stopped when the login cannot be built
        self.stop()
        self.start()

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        else:
            import psutil
            for p in psutil.process_iter(["name"]):
                if (p.info["name"] or "").lower().startswith(self.exe.stem.lower()):
                    p.terminate()
        self._proc = None
        time.sleep(1)

    # --- control API: pause / resume --------------------------------------------------------------
    def _control(self, path: str, body: dict) -> Any:
        code, resp = self._req("POST", path, body)
        if code == 404:
            raise NotImplementedError(f"{path}: control level off (start the miner with --api-control pause)")
        if code == 401:
            raise PermissionError("PEAKMINER_API_KEY rejected by the miner")
        if code == 429:
            raise RuntimeError(f"{path}: debounced, retry later")
        if code // 100 != 2:
            raise RuntimeError(f"{path} {code}: {resp}")
        return resp

    def pause_gpu(self, index: int) -> None:
        self._control("/control/pause", {"gpus": [int(index)]})

    def resume_gpu(self, index: int) -> None:
        self._control("/control/resume", {"gpus": [int(index)]})

    def control_state(self) -> dict | None:
        code, body = self._req("GET", "/control/state")
        return body if code == 200 else None

    # --- control API: overclock (in place, no restart) ----------------------------------------------
    def oc_get(self, index: int) -> dict | None:
        code, body = self._req("GET", "/control/oc")
        if code != 200 or not isinstance(body, list):
            return None
        return next((g for g in body if g.get("id") == index), None)

    def oc_set(self, index: int, **knobs: Any) -> dict:
        """PATCH one or more knobs (core, lcore, mem, lmem, power, fan). Falls back to PUT when the
        running profile came from CLI flags/bench (409). Raises NotImplementedError when the level is off."""
        code, resp = self._req("PATCH", f"/control/oc/{int(index)}", knobs)
        if code == 409:
            cur = (resp or {}).get("profile") if isinstance(resp, dict) else None
            full = dict(cur or {})
            full.update({k: v for k, v in knobs.items() if k != "fan"})
            if "fan" in knobs:
                full["fan"] = knobs["fan"]
            code, resp = self._req("PUT", f"/control/oc/{int(index)}", full)
        if code == 404:
            raise NotImplementedError("/control/oc: oc level off (start the miner with --api-control oc)")
        if code == 401:
            raise PermissionError("PEAKMINER_API_KEY rejected by the miner")
        if code == 429:
            raise RuntimeError("/control/oc: debounced (60 s per GPU), retry later")
        if code == 422:
            raise RuntimeError(f"/control/oc: driver took nothing: {resp}")
        if code // 100 != 2:
            raise RuntimeError(f"/control/oc {code}: {resp}")
        return resp or {}

    def oc_reset(self, index: int) -> None:
        code, resp = self._req("DELETE", f"/control/oc/{int(index)}")
        if code == 404:
            raise NotImplementedError("/control/oc: oc level off")
        if code // 100 != 2 and code != 409:
            raise RuntimeError(f"/control/oc delete {code}: {resp}")
