"""/optimize: time-boxed, one-GPU-at-a-time search over three strategies.

Per GPU: snapshot original settings (saved as known_good if none) -> baseline window -> for each
strategy A/B/C: up to `steps` trials, each held `hold_s` and judged against the baseline; a step that
is unstable or worse than baseline by `worse_margin_pct` is reverted at once and ends that
strategy. Every step is a TrialManager trial, so the watchdog and the boot path revert it if this
process dies. When the window ends, the best verified step for the goal is applied and saved as the
active profile; if nothing beat the baseline, the original settings are restored.

The optimizer samples the GPU itself every 15 s during holds (15 s rows), so it does not depend on
the agent service being up; if the watchdog does escalate on this GPU mid-run, the run aborts.
"""
from __future__ import annotations

import logging
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .actions import Actions
from .collector import Collector
from .config import Config
from .db import Db
from .scoring import Window, compare, score, window_stats
from .trials import TrialManager

log = logging.getLogger(__name__)
GOALS = ("max-hashrate", "max-efficiency", "target-temp")


@dataclass
class StepResult:
    strategy: str
    step: int
    setting: dict
    window: Window
    verdict: str
    delta_pct: float | None
    reasons: list[str]
    trial_id: int | None = None

    def as_dict(self) -> dict:
        return {"strategy": self.strategy, "step": self.step, "setting": self.setting, "window": self.window.as_dict(),
                "verdict": self.verdict, "delta_pct": self.delta_pct, "reasons": self.reasons, "trial_id": self.trial_id}


@dataclass
class GpuReport:
    uuid: str
    goal: str
    original: dict
    baseline: Window
    steps: list[StepResult] = field(default_factory=list)
    winner: StepResult | None = None
    outcome: str = ""              # applied|restored|aborted|refused
    note: str = ""
    started: float = 0.0
    ended: float = 0.0

    def as_dict(self) -> dict:
        return {"uuid": self.uuid, "goal": self.goal, "original": self.original, "baseline": self.baseline.as_dict(),
                "steps": [s.as_dict() for s in self.steps], "winner": self.winner.as_dict() if self.winner else None,
                "outcome": self.outcome, "note": self.note, "started": self.started, "ended": self.ended}


class Optimizer:
    def __init__(self, cfg: Config, db: Db, actions: Actions, trials: TrialManager, collector: Collector,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.db, self.actions, self.trials, self.col = cfg, db, actions, trials, collector
        self.clock, self.sleep = clock, sleep
        self.run_id = "opt-" + uuidlib.uuid4().hex[:8]
        self.deadline_rig = 0.0
        self.reload()

    def reload(self) -> None:
        """Re-read optimize.* from config (CLI flags patch the config before run())."""
        cfg = self.cfg
        o = cfg.get("optimize", {}) or {}
        self.per_gpu_s = float(o.get("per_gpu_s", 2700))
        self.rig_cap_s = float(o.get("rig_cap_s", 10800))
        self.baseline_s = float(o.get("baseline_s", 600))
        self.hold_s = float(o.get("hold_s", 360))
        self.steps = int(o.get("steps", 4))
        self.worse_margin = float(o.get("worse_margin_pct", 3))
        self.settle_s = float(cfg.get("tuning.settle_s", 90))

    # --- strategy step generation ----------------------------------------------------------------
    def strategy_steps(self, uuid: str) -> dict[str, list[dict]]:
        info = self.actions.info(uuid)
        c = self.actions.clamps(uuid)
        strat = self.cfg.get("optimize.strategies", {}) or {}
        strat = {**strat, **(c.get("optimize_strategies") or {})}
        stock = info.stock_pl_w or 0
        lo_pl = max(stock * c["power_limit_pct"][0] / 100, info.min_pl_w or 0)
        hi_pl = min(stock * c["power_limit_pct"][1] / 100, info.max_pl_w or 1e9)

        def pl(pct):
            return round(min(hi_pl, max(lo_pl, stock * pct / 100)), 1)

        def lst(v, n):
            return list(v) if isinstance(v, list) else [v] * n

        def mem_lock(v):
            if v is None:
                return None
            lo, hi = c.get("mem_lock_mhz", [5001, 10501])
            return int(min(max(int(v), lo), hi))

        out: dict[str, list[dict]] = {}
        a = strat.get("A", {})
        if a:
            n = self.steps
            pls, locks = lst(a.get("pl_pct", 70), n), lst(a.get("core_lock_mhz", 1400), n)
            out["A"] = [{"pl_w": pl(pls[i]), "core_lock_mhz": int(min(max(locks[i], c["core_lock_mhz"][0]), c["core_lock_mhz"][1])),
                         "core_offset_mhz": 0, "mem_offset_mhz": int(min(a.get("mem_offset_mhz", 0), c["mem_offset_mhz"][1])),
                         "mem_lock_mhz": mem_lock(a.get("mem_lock_mhz"))}
                        for i in range(min(n, len(pls), len(locks)))]
        b = strat.get("B", {})
        if b:
            n = self.steps
            cores, mems = lst(b.get("core_offset_mhz", 0), n), lst(b.get("mem_offset_mhz", 0), n)
            out["B"] = [{"pl_w": pl(b.get("pl_pct", 100)), "core_lock_mhz": None,
                         "core_offset_mhz": int(min(cores[i], c["core_offset_mhz"][1])),
                         "mem_offset_mhz": int(min(mems[i], c["mem_offset_mhz"][1])),
                         "mem_lock_mhz": mem_lock(b.get("mem_lock_mhz"))}
                        for i in range(min(n, len(cores), len(mems)))]
        cc = strat.get("C", {})
        if cc:
            n = self.steps
            pls = lst(cc.get("pl_pct", 75), n)
            locks = lst(cc.get("core_lock_mhz"), n)
            out["C"] = [{"pl_w": pl(pls[i]), "core_lock_mhz": None if locks[i] is None else int(min(max(locks[i], c["core_lock_mhz"][0]), c["core_lock_mhz"][1])),
                         "core_offset_mhz": 0, "mem_offset_mhz": int(min(cc.get("mem_offset_mhz", 0), c["mem_offset_mhz"][1])),
                         "mem_lock_mhz": mem_lock(cc.get("mem_lock_mhz"))}
                        for i in range(min(n, len(pls), len(locks)))]
        return out

    # --- sampling during holds -------------------------------------------------------------------
    def _hold(self, uuid: str, seconds: float, gpu_id: int) -> None:
        """Sleep `seconds` while writing 15 s samples for this GPU; abort if the watchdog steps in."""
        end = self.clock() + seconds
        infos = [i for i in self.col.register_gpus() if i.uuid == uuid]
        last = False
        while not last:
            last = self.clock() >= end            # always take one closing sample at the end of the hold
            rows = self.col.sample_gpus(infos)
            for info, s in rows:
                if s is None:
                    raise RuntimeError(f"GPU {uuid} unreadable during optimize")
                cols = s.as_cols()
                cols["hashrate"], cols["accepted"], cols["rejected"] = (s.extra.pop("hashrate", None), s.extra.pop("accepted", None),
                                                                        s.extra.pop("rejected", None))
                self.db.insert_gpu_sample(gpu_id, self.clock(), "15s", **cols)
            wd = self.db.kv_get("watchdog.state") or {}
            if (wd.get("ladder") or {}).get(uuid):
                raise RuntimeError(f"watchdog protection active on {uuid}: {wd['ladder'][uuid]}")
            if self.clock() >= self.deadline_rig:
                raise TimeoutError("rig-wide cap reached")
            if not last:
                self.sleep(min(15.0, max(0.0, end - self.clock())))

    # --- one GPU -----------------------------------------------------------------------------------
    def optimize_gpu(self, uuid: str, goal: str) -> GpuReport:
        gid = self.db.gpu_id(uuid)
        now = self.clock()
        orig = self.actions.settings(uuid)
        rep = GpuReport(uuid=uuid, goal=goal, original=orig, baseline=Window(), started=now)
        if gid is None or not self.cfg.gpu_control_enabled(uuid):
            rep.outcome, rep.note = "refused", "control_enabled=false"
            return rep
        if self.cfg.gpu_external_oc(uuid):
            rep.outcome, rep.note = "refused", f"clocks owned by {self.cfg.gpu_external_oc(uuid)}; remove gpus[].external_oc to let the tool tune"
            return rep
        wd = self.db.kv_get("watchdog.state") or {}
        if (wd.get("ladder") or {}).get(uuid) or self.db.incident_open_for("overtemp", gid):
            rep.outcome, rep.note = "refused", "GPU is under watchdog protection or has an open incident"
            return rep
        self.trials.abort_all("optimize start", actor="optimizer", gpu_id=gid)
        if not self.db.profile(gid, "known_good"):
            self.db.save_profile(gid, orig, "known_good", created_by="optimizer", note="snapshot before optimize")
        deadline = min(now + self.per_gpu_s, self.deadline_rig)
        targets = self.cfg.targets_for(uuid)
        try:
            # baseline
            self._hold(uuid, min(self.baseline_s, max(0.0, deadline - self.clock())), gid)
            rep.baseline = self.trials.window(gid, now, self.clock())
            if rep.baseline.n < 4:
                raise RuntimeError("baseline too short")
            strategies = self.strategy_steps(uuid)
            names = list(strategies)
            for si, name in enumerate(names):
                remaining = deadline - self.clock()
                per_strategy = remaining / max(1, len(names) - si)
                budget_steps = int(per_strategy // (self.hold_s + 30))
                for k, setting in enumerate(strategies[name][:max(0, min(self.steps, budget_steps))]):
                    if self.clock() + self.hold_s > deadline:
                        break
                    # deadline = fail-safe only: if this process dies, the watchdog reverts the step soon after
                    t = self.trials.start(uuid, setting, self.hold_s + 120, kind="optimize", actor="optimizer",
                                          reason=f"optimize {name}{k + 1}", run_id=self.run_id, strategy=name, step=k + 1,
                                          goal=goal, baseline=rep.baseline, stepped=True)
                    if "error" in t:
                        rep.steps.append(StepResult(name, k + 1, setting, Window(), "refused", None, [t["error"]]))
                        break
                    t0 = self.clock()
                    self._hold(uuid, self.hold_s, gid)
                    w = self.trials.window(gid, t0 + self.settle_s, self.clock())
                    v = compare(rep.baseline, w, goal=goal, targets=targets, min_improve_pct=0.5,
                                worse_margin_pct=self.worse_margin)
                    sr = StepResult(name, k + 1, setting, w, v.verdict, v.delta_pct, v.reasons, t["id"])
                    rep.steps.append(sr)
                    # every step returns to the original settings before the next one; the winner is
                    # re-applied at the end, so no trial setting is ever left running
                    trial = next(x for x in self.db.open_trials(gid) if x["id"] == t["id"])
                    self.trials.revert(trial, "candidate" if v.verdict == "better" else "reverted", sr.as_dict(),
                                       "; ".join(v.reasons), actor="optimizer", run_id=self.run_id)
                    log.info("optimize %s %s%d %s -> %s %s", uuid, name, k + 1, setting, v.verdict, v.reasons)
                    if v.verdict in ("unstable", "worse", "insufficient"):
                        break
            self._finish(rep, gid, goal, targets)
        except (TimeoutError, RuntimeError, KeyboardInterrupt) as e:
            rep.note = f"{type(e).__name__}: {e}"
            try:
                self.trials.abort_all(f"optimize abort: {e}", actor="optimizer", gpu_id=gid)
                self.actions.apply_settings(uuid, orig, "optimizer", f"optimize abort: restore original ({e})", run_id=self.run_id)
            finally:
                rep.outcome = "aborted"
        rep.ended = self.clock()
        self.db.kv_set(f"optimize.last.{uuid}", rep.as_dict())
        return rep

    def _finish(self, rep: GpuReport, gid: int, goal: str, targets: dict) -> None:
        cands = [s for s in rep.steps if s.verdict == "better"]
        if goal == "target-temp":
            cands = [s for s in cands if s.window.core_max is not None and s.window.core_max <= targets.get("core_c_max", 75)]
        if cands:
            rep.winner = max(cands, key=lambda s: score(s.window, goal) or 0)
            full = {**rep.original, **rep.winner.setting}
            res = self.actions.apply_settings(rep.uuid, full, "optimizer", f"optimize winner {rep.winner.strategy}{rep.winner.step}",
                                              run_id=self.run_id)
            if all(r.ok for r in res):
                self.db.save_profile(gid, full, "active", goal=goal, score=rep.winner.window.as_dict(), created_by="optimizer",
                                     note=f"optimize {self.run_id}: {rep.winner.strategy}{rep.winner.step} {rep.winner.delta_pct:+.1f}%")
                rep.outcome = "applied"
                return
            rep.note = "winner apply failed: " + "; ".join(r.error or "" for r in res if not r.ok)
        self.actions.apply_settings(rep.uuid, rep.original, "optimizer", "optimize: nothing beat baseline, restoring", run_id=self.run_id)
        rep.outcome = "restored"

    # --- rig -----------------------------------------------------------------------------------------
    def run(self, uuids: list[str], goal: str | None = None) -> list[GpuReport]:
        goal = goal or str(self.cfg.get("optimize.default_goal", "max-efficiency"))
        if goal not in GOALS:
            raise ValueError(f"goal must be one of {GOALS}")
        self.reload()
        self.deadline_rig = self.clock() + self.rig_cap_s
        reports = []
        for u in uuids:                               # strictly one GPU at a time
            if self.clock() + self.baseline_s + self.hold_s > self.deadline_rig:
                log.warning("rig cap reached before %s; skipped", u)
                break
            reports.append(self.optimize_gpu(u, goal))
        self.db.kv_set("optimize.last_run", {"run_id": self.run_id, "goal": goal, "ts": self.clock(),
                                             "gpus": [r.as_dict() for r in reports]})
        return reports


def format_report(reports: list[GpuReport]) -> str:
    """Before/after table for the CLI and the slash command."""
    lines = []
    for r in reports:
        lines.append(f"GPU {r.uuid}  goal={r.goal}  outcome={r.outcome}  {r.note}".rstrip())
        b = r.baseline
        lines.append(f"  {'':10} {'hashrate':>12} {'power':>8} {'eff':>10} {'core':>6} {'memj':>6} {'rej':>6}  settings")
        lines.append(f"  {'baseline':10} {_hr(b.hashrate):>12} {_w(b.power_w):>8} {_eff(b.eff):>10} {_c(b.core_max):>6} {_c(b.memj_max):>6} "
                     f"{_rr(b.rej_rate):>6}  {r.original}")
        for s in r.steps:
            tag = f"{s.strategy}{s.step} {s.verdict}" + (" *" if r.winner is s else "")
            w = s.window
            lines.append(f"  {tag:10} {_hr(w.hashrate):>12} {_w(w.power_w):>8} {_eff(w.eff):>10} {_c(w.core_max):>6} {_c(w.memj_max):>6} "
                         f"{_rr(w.rej_rate):>6}  {s.setting}  {s.delta_pct if s.delta_pct is None else f'{s.delta_pct:+.1f}%'}")
        if r.winner:
            lines.append(f"  applied: {r.winner.strategy}{r.winner.step} ({r.winner.delta_pct:+.1f}% on {r.goal})")
        lines.append("")
    return "\n".join(lines)


def _hr(v):
    if v is None:
        return "-"
    for u, f in (("TH/s", 1e12), ("GH/s", 1e9), ("MH/s", 1e6), ("kH/s", 1e3)):
        if v >= f:
            return f"{v / f:.2f} {u}"
    return f"{v:.0f} H/s"


def _w(v):
    return "-" if v is None else f"{v:.0f} W"


def _eff(v):
    return "-" if v is None else _hr(v) + "/W"


def _c(v):
    return "-" if v is None else f"{v:.0f}C"


def _rr(v):
    return "-" if v is None else f"{v * 100:.1f}%"
