"""Hourly pre-check (deterministic). Decides whether any GPU is a tuning candidate and, if so,
proposes one small step. Claude reviews this context and may start exactly one trial.

Triggers: core/memory temperature above target, hashrate or efficiency below the GPU's baseline by
a margin, instability (reject rate, hardware throttle flags), or a coin change since the profile
was saved. GPUs without a trigger are never touched. Converged GPUs are excluded until a trigger
fires or their converged_until passes. Trials are capped per GPU per day.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .actions import Actions
from .config import Config
from .db import Db
from .scoring import Window, window_stats


class Tuner:
    def __init__(self, cfg: Config, db: Db, actions: Actions, clock=time.time):
        self.cfg, self.db, self.actions, self.clock = cfg, db, actions, clock
        self.h = cfg.get("tuning.hourly", {}) or {}

    def last_hour(self, gpu_id: int) -> Window:
        now = self.clock()
        rows = self.db.query("SELECT * FROM gpu_samples WHERE gpu_id=? AND res='5m' AND ts>=? ORDER BY ts", (gpu_id, now - 3600))
        if len(rows) < 4:            # thin hour (fresh start): fold in the 15 s ring
            rows = self.db.query("SELECT * FROM gpu_samples WHERE gpu_id=? AND ts>=? ORDER BY ts", (gpu_id, now - 3600))
        return window_stats(rows)

    def baseline_for(self, gpu_id: int) -> tuple[dict | None, Window | None]:
        """The saved profile's score window is the baseline; falls back to the prior 24 h of 5 m rows."""
        for st in ("converged", "active", "known_good"):
            p = self.db.profile(gpu_id, st)
            if p and p.get("score") and p["score"].get("n"):
                return p, Window(**{k: v for k, v in p["score"].items() if k in Window.__dataclass_fields__})
        now = self.clock()
        rows = self.db.query("SELECT * FROM gpu_samples WHERE gpu_id=? AND res='5m' AND ts>=? AND ts<? ORDER BY ts",
                             (gpu_id, now - 25 * 3600, now - 3600))
        w = window_stats(rows)
        return (self.db.profile(gpu_id, "active") or self.db.profile(gpu_id, "known_good")), (w if w.n >= 4 else None)

    def evaluate(self, uuid: str, coin: str | None) -> dict[str, Any]:
        gid = self.db.gpu_id(uuid)
        g = self.db.one("SELECT * FROM gpus WHERE id=?", (gid,))
        t = self.cfg.targets_for(uuid)
        now = self.clock()
        cur = self.last_hour(gid)
        prof, base = self.baseline_for(gid)
        triggers: list[str] = []
        if cur.n == 0:
            return {"uuid": uuid, "idx": g["idx"], "model": g["model"], "candidate": False, "reason": "no samples in the last hour",
                    "triggers": [], "current": cur.as_dict()}
        over_c = float(self.h.get("trigger_temp_over_c", 0))
        if cur.core_max is not None and cur.core_max > t.get("core_c_max", 75) + over_c:
            triggers.append(f"core {cur.core_max:.0f}C > target {t.get('core_c_max')}C")
        if cur.memj_max is not None and cur.memj_max > t.get("memj_c_max", 100) + over_c:
            triggers.append(f"memj {cur.memj_max:.0f}C > target {t.get('memj_c_max')}C")
        if base and base.hashrate and cur.hashrate is not None:
            drop = (base.hashrate - cur.hashrate) / base.hashrate * 100
            if drop > float(self.h.get("trigger_hashrate_drop_pct", 5)):
                triggers.append(f"hashrate {drop:.1f}% below baseline")
        if base and base.eff and cur.eff is not None:
            drop = (base.eff - cur.eff) / base.eff * 100
            if drop > float(self.h.get("trigger_eff_drop_pct", 5)):
                triggers.append(f"efficiency {drop:.1f}% below baseline")
        if cur.rej_rate is not None and cur.rej_rate > 0.02:
            triggers.append(f"reject rate {cur.rej_rate * 100:.1f}%")
        if cur.throttle_hits:
            triggers.append(f"hw throttle flags in {cur.throttle_hits} samples")
        if prof and prof.get("coin") and coin and prof["coin"] != coin:
            triggers.append(f"coin changed {prof['coin']} -> {coin}")

        # exclusions
        excl: list[str] = []
        if not self.cfg.gpu_control_enabled(uuid):
            excl.append("control_enabled=false")
        conv = self.db.profile(gid, "converged")
        if conv and conv.get("converged_until") and conv["converged_until"] > now and not triggers:
            excl.append(f"converged until {time.strftime('%Y-%m-%d', time.localtime(conv['converged_until']))}")
        n_today = len(self.db.trials_since(gid, now - 86400, kind="hourly"))
        cap = int(self.h.get("max_trials_per_gpu_per_day", 6))
        if n_today >= cap:
            excl.append(f"trial cap {n_today}/{cap} today")
        if self.db.open_trials(gid):
            excl.append("trial already open")
        wd = self.db.kv_get("watchdog.state") or {}
        if (wd.get("ladder") or {}).get(uuid):
            excl.append(f"watchdog protection: {wd['ladder'][uuid]}")

        severity = self._severity(cur, t, base)
        proposal = self.propose(uuid, cur, t, base, triggers) if triggers and not excl else None
        return {"uuid": uuid, "idx": g["idx"], "model": g["model"], "candidate": bool(triggers) and not excl,
                "triggers": triggers, "excluded": excl, "severity": severity, "current": cur.as_dict(),
                "baseline": base.as_dict() if base else None, "profile": {"status": prof["status"], "settings": prof["settings"]} if prof else None,
                "settings": self.actions.settings(uuid), "trials_today": n_today, "proposal": proposal}

    @staticmethod
    def _severity(cur: Window, t: dict, base: Window | None) -> float:
        """Worst-offender score: degrees over target plus percent below baseline."""
        s = 0.0
        if cur.core_max is not None:
            s += max(0.0, cur.core_max - t.get("core_c_max", 75)) * 2
        if cur.memj_max is not None:
            s += max(0.0, cur.memj_max - t.get("memj_c_max", 100)) * 2
        if base and base.eff and cur.eff is not None:
            s += max(0.0, (base.eff - cur.eff) / base.eff * 100)
        if cur.rej_rate:
            s += cur.rej_rate * 100
        return round(s, 2)

    def propose(self, uuid: str, cur: Window, t: dict, base: Window | None, triggers: list[str]) -> dict:
        """One small, clamp-respecting step. Temperature first; then efficiency/hashrate."""
        info = self.actions.info(uuid)
        c = self.actions.clamps(uuid)
        s = self.actions.settings(uuid)
        stock = info.stock_pl_w or s["pl_w"] or 0
        step_w = stock * c["power_limit_step_pct"] / 100
        lo = max(stock * c["power_limit_pct"][0] / 100, info.min_pl_w or 0)
        hot = any("C > target" in x for x in triggers)
        if hot:
            if s.get("fan_pct") is not None and s["fan_pct"] < c["fan_pct"][1]:
                return {"key": "fan_pct", "value": min(c["fan_pct"][1], s["fan_pct"] + c["fan_step_pct"]), "why": "temperature over target: more fan first"}
            if (s.get("mem_offset_mhz") or 0) > 0 and cur.memj_max and cur.memj_max > t.get("memj_c_max", 100):
                return {"key": "mem_offset_mhz", "value": max(c["mem_offset_mhz"][0], s["mem_offset_mhz"] - c["mem_step_mhz"]), "why": "memory junction over target: lower memory offset"}
            if s.get("core_lock_mhz") and s["core_lock_mhz"] - 50 >= c["core_lock_mhz"][0]:
                return {"key": "core_lock_mhz", "value": int(s["core_lock_mhz"] - 50), "why": "temperature over target: lower the core clock lock 50 MHz"}
            if s["pl_w"] and s["pl_w"] - step_w >= lo:
                return {"key": "pl_w", "value": round(s["pl_w"] - step_w, 1), "why": "temperature over target: lower power limit one step"}
            return {"key": None, "value": None, "why": "already at the power floor; escalate to Claude/owner (fan curve, airflow)"}
        if any("reject" in x or "throttle" in x for x in triggers):
            if (s.get("mem_offset_mhz") or 0) > 0:
                return {"key": "mem_offset_mhz", "value": s["mem_offset_mhz"] - c["mem_step_mhz"], "why": "instability: back off memory offset"}
            if (s.get("core_offset_mhz") or 0) > 0:
                return {"key": "core_offset_mhz", "value": s["core_offset_mhz"] - c["core_step_mhz"], "why": "instability: back off core offset"}
        # below baseline: nudge toward the saved profile's power limit if we are under it, else small mem step up
        if base and self.db.profile(self.db.gpu_id(uuid), "active"):
            prof = self.db.profile(self.db.gpu_id(uuid), "active")["settings"]
            if prof.get("pl_w") and s["pl_w"] and s["pl_w"] < prof["pl_w"]:
                return {"key": "pl_w", "value": round(min(prof["pl_w"], s["pl_w"] + step_w), 1), "why": "below baseline and under the profile's power limit (likely left at the watchdog floor)"}
        if (s.get("mem_offset_mhz") or 0) + c["mem_step_mhz"] <= c["mem_offset_mhz"][1]:
            return {"key": "mem_offset_mhz", "value": (s.get("mem_offset_mhz") or 0) + c["mem_step_mhz"], "why": "below baseline: try one memory step up"}
        return {"key": None, "value": None, "why": "no safe single step available"}

    def precheck(self, coin: str | None) -> dict[str, Any]:
        gpus = self.db.gpus_for_host(self.db.one("SELECT id FROM hosts WHERE name=?", (self.cfg.host_name,))["id"])
        evals = [self.evaluate(g["uuid"], coin) for g in gpus]
        cands = sorted([e for e in evals if e["candidate"]], key=lambda e: -e["severity"])
        max_per_run = int(self.h.get("max_gpus_per_run", 1))
        return {"ts": self.clock(), "coin": coin, "gpus": evals, "candidates": [e["uuid"] for e in cands[:max_per_run]],
                "skipped_candidates": [e["uuid"] for e in cands[max_per_run:]],
                "hold_s": int(self.h.get("trial_hold_s", 900)), "max_gpus_per_run": max_per_run}
