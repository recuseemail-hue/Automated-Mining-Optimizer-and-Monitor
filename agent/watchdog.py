"""Local watchdog: 15-second poll, no network, no LLM.

Rules (config `watchdog`): sustained core/memory over-temperature, fan failure under load, GPU
missing / driver reset, zero hashrate or miner down for 10 min, power above budget, hard-limit
breach. Each GPU has a protective ladder: 1) power limit to the floor, 2) pause that GPU in the
miner, 3) stop the miner. Hard limits shut the PC down (after a push notification). Recovery is
automatic after the condition has been clear for `recover_after_s`, capped per day.

The watchdog also writes the 15 s telemetry ring and runs TrialManager.tick(), so a trial past its
window is reverted within 15 s even if nothing else is alive.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .actions import Actions
from .collector import Collector
from .config import Config
from .db import Db
from .notify import Notifier
from .trials import TrialManager

log = logging.getLogger(__name__)

LADDER = ("none", "power_floor", "paused", "miner_stopped", "shutdown")


@dataclass
class GpuState:
    since: dict[str, float] = field(default_factory=dict)      # rule -> first time seen true
    clear_since: float | None = None
    ladder: int = 0
    ladder_at: float = 0.0
    active_rule: str | None = None


class Watchdog:
    def __init__(self, cfg: Config, db: Db, collector: Collector, actions: Actions, trials: TrialManager,
                 notifier: Notifier, clock=time.time, on_incident=None):
        self.cfg, self.db, self.col, self.actions, self.trials, self.notifier = cfg, db, collector, actions, trials, notifier
        self.clock = clock
        self.on_incident = on_incident           # M6 hook: called with incident id after the local ladder acted
        self.w = cfg.get("watchdog", {})
        self.gpus: dict[str, GpuState] = {}
        self.host_id = collector.host_id
        self.miner_down_since: float | None = None
        self.last_restart_ts: float = -1e12
        self.expected_gpus: set[str] = set(self.db.kv_get("watchdog.expected_gpus", []) or [])
        self.last_tick: dict[str, Any] = {}

    # --- helpers --------------------------------------------------------------------------
    def _st(self, uuid: str) -> GpuState:
        return self.gpus.setdefault(uuid, GpuState())

    def _sustained(self, st: GpuState, rule: str, cond: bool, hold_s: float) -> bool:
        now = self.clock()
        if cond:
            st.since.setdefault(rule, now)
            return now - st.since[rule] >= hold_s
        st.since.pop(rule, None)
        return False

    def _incident(self, rule: str, severity: str, uuid: str | None, summary: str, ladder: str) -> int:
        gid = self.db.gpu_id(uuid) if uuid else None
        existing = self.db.incident_open_for(rule, gid)
        if existing:
            self.db.update_incident(existing["id"], ladder_step=ladder, summary=summary)
            return existing["id"]
        iid = self.db.open_incident(rule, severity, host_id=self.host_id, gpu_id=gid, ladder_step=ladder, summary=summary,
                                    opened=self.clock())
        if self.cfg.role == "worker":
            self.db.outbox_push("incident", {"local_id": iid, "opened": self.clock(), "gpu_uuid": uuid, "rule": rule,
                                             "severity": severity, "ladder_step": ladder, "status": "open", "summary": summary})
        self.notifier.push(f"{self.cfg.host_name}: {rule}", f"{summary}\naction: {ladder}",
                           priority="urgent" if severity == "critical" else "high", tags="rotating_light",
                           key=f"incident.{rule}.{uuid}")
        if self.on_incident:
            try:
                self.on_incident(iid)
            except Exception:
                log.exception("incident hook failed")
        return iid

    def _recoveries_today(self, uuid: str) -> int:
        return self.db.count_actions_since("miner_resume_gpu", self.clock() - 86400, host_id=self.host_id,
                                           gpu_id=self.db.gpu_id(uuid))

    # --- ladder -----------------------------------------------------------------------------
    def escalate(self, uuid: str, rule: str, summary: str, *, severity: str = "critical") -> None:
        st = self._st(uuid)
        now = self.clock()
        if st.ladder > 0 and now - st.ladder_at < float(self.w.get("escalate_after_s", 60)):
            return                                        # give the previous step time to work
        st.active_rule = rule
        st.clear_since = None
        self.trials.abort_all(f"watchdog {rule}", gpu_id=self.db.gpu_id(uuid))
        step = st.ladder + 1
        if step == 1:
            info = self.actions.info(uuid)
            floor = (info.stock_pl_w or 0) * float(self.w.get("ladder_pl_floor_pct", 55)) / 100
            lo = max(floor, info.min_pl_w or 0)
            self.actions.set_power_limit(uuid, round(lo, 1), "rule", f"{rule}: {summary}", emergency=True)
        elif step == 2:
            r = self.actions.miner_pause_gpu(uuid, "rule", f"{rule}: {summary}", emergency=True)
            if not r.ok:                                  # miner cannot pause one GPU -> go straight to stop
                step = 3
                self.actions.miner_stop("rule", f"{rule}: {summary} (pause unsupported)", emergency=True)
        elif step == 3:
            self.actions.miner_stop("rule", f"{rule}: {summary}", emergency=True)
        else:
            step = 4
            self.actions.shutdown("rule", f"{rule} persists after miner stop: {summary}", emergency=True)
        st.ladder, st.ladder_at = step, now
        self._incident(rule, severity, uuid, summary, LADDER[step])
        log.warning("ladder %s -> %s for %s (%s)", LADDER[step - 1] if step else "none", LADDER[step], uuid, summary)

    def hard_stop(self, uuid: str | None, rule: str, summary: str) -> None:
        st = self._st(uuid) if uuid else None
        self.trials.abort_all(f"watchdog {rule}")
        self.actions.miner_stop("rule", f"{rule}: {summary}", emergency=True)
        if st:
            st.ladder, st.ladder_at, st.active_rule = 4, self.clock(), rule
        self._incident(rule, "critical", uuid, summary, "shutdown")
        self.actions.shutdown("rule", f"{rule}: {summary}", emergency=True)

    def maybe_recover(self, uuid: str, all_clear: bool) -> None:
        st = self._st(uuid)
        if st.ladder == 0 or st.ladder >= 4:
            return
        now = self.clock()
        if not all_clear:
            st.clear_since = None
            return
        st.clear_since = st.clear_since or now
        if now - st.clear_since < float(self.w.get("recover_after_s", 600)):
            return
        if self._recoveries_today(uuid) >= int(self.w.get("max_auto_recoveries_per_day", 3)):
            self._incident("recovery_cap", "high", uuid, "auto-recovery cap reached; staying protected", LADDER[st.ladder])
            return
        rule = st.active_rule
        if st.ladder >= 3:
            self.actions.miner_start("rule", f"recover after {rule}", emergency=True)
        if st.ladder >= 2:
            self.actions.miner_resume_gpu(uuid, "rule", f"recover after {rule}", emergency=True)
        # the lowered power limit stays until the hourly tuner or Claude restores it deliberately
        gid = self.db.gpu_id(uuid)
        inc = self.db.incident_open_for(rule, gid) if rule else None
        if inc:
            self.db.update_incident(inc["id"], status="recovered", closed=now)
        self.notifier.push(f"{self.cfg.host_name}: recovered", f"GPU {uuid} resumed after {rule}; power limit left at floor",
                           priority="default", tags="white_check_mark", key=f"recover.{uuid}")
        st.ladder, st.active_rule, st.clear_since = 0, None, None
        st.since.clear()

    # --- one poll ---------------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        now = self.clock()
        infos = self.col.register_gpus()
        present = {i.uuid for i in infos}
        mstats = self.col.miner.stats()
        rows = self.col.sample_gpus(infos, mstats)
        self._write_fast_rows(rows, now)

        # GPU missing / driver reset
        missing = self.expected_gpus - present
        for uuid in missing:
            st = self._st(uuid)
            if self._sustained(st, "gpu_missing", True, 30) and st.ladder < 3:
                self.trials.abort_all("gpu missing", gpu_id=self.db.gpu_id(uuid))
                self.actions.miner_stop("rule", f"GPU {uuid} missing from NVML (driver reset / fell off bus)", emergency=True)
                st.ladder, st.ladder_at, st.active_rule = 3, now, "gpu_missing"
                self._incident("gpu_missing", "critical", uuid, "GPU no longer enumerated by the driver", "miner_stopped")
        if present and not missing:
            self.expected_gpus |= present
            self.db.kv_set("watchdog.expected_gpus", sorted(self.expected_gpus))

        total_w = 0.0
        for info, s in rows:
            if s is None:
                continue
            st = self._st(info.uuid)
            total_w += s.power_w or 0.0
            t = self.cfg.targets_for(info.uuid)
            core_hard, memj_hard = float(self.w.get("core_c_hard", 95)), float(self.w.get("memj_c_hard", 110))
            core_crit, memj_crit = float(self.w.get("core_c_crit", 85)), float(self.w.get("memj_c_crit", 105))
            sustain = float(self.w.get("sustain_s", 30))

            if (s.core_c is not None and s.core_c >= core_hard) or (s.memj_c is not None and s.memj_c >= memj_hard):
                if self._sustained(st, "hard_temp", True, sustain) and st.ladder < 4:
                    self.hard_stop(info.uuid, "hard_temp", f"core {s.core_c}C memj {s.memj_c}C >= hard limit")
                    continue
            else:
                st.since.pop("hard_temp", None)

            over = (s.core_c is not None and s.core_c >= core_crit) or (s.memj_c is not None and s.memj_c >= memj_crit)
            if self._sustained(st, "overtemp", over, sustain):
                self.escalate(info.uuid, "overtemp", f"core {s.core_c:.0f}C memj {s.memj_c and round(s.memj_c)} for {sustain:.0f}s")

            loaded = (s.util_pct or 0) >= float(self.w.get("fan_fail_util_pct", 50))
            fan_dead = loaded and ((s.fan_rpm is not None and s.fan_rpm <= 0) or
                                   (s.fan_rpm is None and s.fan_pct is not None and s.fan_pct <= 0 and (s.core_c or 0) > 60))
            if self._sustained(st, "fan_failure", fan_dead, float(self.w.get("fan_fail_sustain_s", 60))):
                self.escalate(info.uuid, "fan_failure", f"fan 0 RPM under load at {s.core_c}C")

            all_clear = not over and not fan_dead and (s.core_c or 0) < t.get("core_c_max", 75)
            self.maybe_recover(info.uuid, all_clear)

        # power above budget: trim the hungriest GPU first, one step per escalate window
        budget = min(float(self.cfg.get("limits.psu_budget_w", 1e9)), float(self.cfg.get("limits.circuit_budget_w", 1e9)))
        host_st = self._st("__host__")
        if self._sustained(host_st, "overpower", total_w > budget, float(self.w.get("overpower_sustain_s", 30))):
            if now - host_st.ladder_at >= float(self.w.get("escalate_after_s", 60)):
                hungry = max((r for r in rows if r[1] is not None), key=lambda r: r[1].power_w or 0, default=None)
                if hungry:
                    info, s = hungry
                    c = self.actions.clamps(info.uuid)
                    stock = info.stock_pl_w or s.pl_w or 0
                    target = max(stock * c["power_limit_pct"][0] / 100, (s.pl_w or stock) - stock * c["power_limit_step_pct"] / 100)
                    self.actions.set_power_limit(info.uuid, round(target, 1), "rule",
                                                 f"overpower: {total_w:.0f}W > budget {budget:.0f}W", emergency=True)
                    host_st.ladder_at = now
                    self._incident("overpower", "high", None, f"total GPU power {total_w:.0f}W > {budget:.0f}W", "power_floor")

        # zero hashrate / miner down
        hr = mstats.total_hashrate if mstats.state == "running" else 0.0
        if mstats.state == "unauthorized":
            self._incident("miner_api_auth", "high", None, mstats.error or "miner API key mismatch", "none")
        down = mstats.state in ("stopped", "unreachable", "error") or (mstats.state == "running" and (hr or 0) <= 0)
        protected = any(st.ladder >= 2 for u, st in self.gpus.items() if u != "__host__")
        if not self.cfg.get("miner.managed", True):
            # someone else runs the miner: never start it, but a process that is alive at 0 H/s (driver reset
            # with the miner's own watchdog disabled) is kicked so its launcher relaunches it
            down = bool(self.cfg.get("miner.kill_stalled_external", True)) and mstats.state == "running" and (hr or 0) <= 0
        if down and not protected:
            self.miner_down_since = self.miner_down_since or now
            # a miner that dies again soon after a rule restart is crash-looping: retry fast so the
            # restart cap (limits.miner_restarts_per_30min) is what ends it, not a 10-minute wait each time
            looping = now - self.last_restart_ts < float(self.w.get("crash_loop_window_s", 900))
            wait = float(self.w.get("crash_loop_retry_s", 60)) if looping else float(self.w.get("zero_hashrate_s", 600))
            if not self.cfg.get("miner.managed", True):
                wait = float(self.w.get("zero_hashrate_s", 600))   # an external launcher needs time to relaunch: no fast retries
            if now - self.miner_down_since >= wait:
                r = self.actions.miner_restart("rule", f"miner {mstats.state}, hashrate {hr} for {wait:.0f}s", emergency=True)
                self.miner_down_since = now
                self.last_restart_ts = now
                if not r.ok:
                    self._incident("miner_down", "high", None, f"miner {mstats.state}; restart refused: {r.error}", "none")
                    if "RateLimited" in (r.error or ""):
                        self.actions.miner_stop("rule", "crash loop: restart cap reached", emergency=True)
        else:
            self.miner_down_since = None

        # trial deadlines (second reverter after TrialManager's own caller)
        judged = self.trials.tick()

        self.last_tick = {"ts": now, "gpus": len(present), "missing": sorted(missing), "total_w": total_w,
                          "miner": mstats.state, "ladder": {u: LADDER[st.ladder] for u, st in self.gpus.items() if st.ladder},
                          "rules": {u: sorted(st.since) for u, st in self.gpus.items() if st.since},
                          "judged": judged}
        self.db.kv_set("watchdog.state", self.last_tick)
        if self.cfg.role == "worker" and (self.last_tick["ladder"] or int(now) % 60 < float(self.w.get("interval_s", 15))):
            self.db.outbox_push("state", {"key": "watchdog.state", "value": self.last_tick})
        return self.last_tick

    def _write_fast_rows(self, rows, ts: float) -> None:
        for info, s in rows:
            if s is None:
                continue
            gid = self.db.gpu_id(info.uuid)
            if gid is None:
                continue
            cols = s.as_cols()
            cols["hashrate"] = s.extra.pop("hashrate", None)
            cols["accepted"] = s.extra.pop("accepted", None)
            cols["rejected"] = s.extra.pop("rejected", None)
            self.db.insert_gpu_sample(gid, ts, "15s", **cols)

    def run_loop(self, stop_event) -> None:
        interval = float(self.w.get("interval_s", 15))
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("watchdog tick failed")
            try:
                self.notifier.flush()
            except Exception:
                pass
            stop_event.wait(interval)
