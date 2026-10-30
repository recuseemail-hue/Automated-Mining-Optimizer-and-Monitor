"""Trial manager: bounded, self-reverting setting changes.

start(): snapshot pre-change settings + baseline window, apply ONE setting change, store a deadline.
tick():  for each open trial past its deadline, judge deterministically and keep or revert.
abort(): revert every open trial (service boot, watchdog emergency, operator).

Nothing here waits for the hourly Claude run; the watchdog calls tick() every 15 s, and the service
calls abort_all('boot') before anything else at start-up, so a crash mid-trial is always undone.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .actions import Actions
from .config import Config
from .db import Db
from .scoring import Window, compare, window_stats

log = logging.getLogger(__name__)


class TrialManager:
    def __init__(self, cfg: Config, db: Db, actions: Actions, clock=time.time):
        self.cfg, self.db, self.actions, self.clock = cfg, db, actions, clock
        self.settle_s = float(cfg.get("tuning.settle_s", 90))

    # --- windows ---------------------------------------------------------------------------
    def window(self, gpu_id: int, start: float, end: float, res: str = "15s") -> Window:
        rows = self.db.query("SELECT * FROM gpu_samples WHERE gpu_id=? AND res=? AND ts>=? AND ts<=? ORDER BY ts",
                             (gpu_id, res, start, end))
        if len(rows) < 4 and res == "15s":   # fall back to the 5-minute rows if the fast ring is thin
            rows = self.db.query("SELECT * FROM gpu_samples WHERE gpu_id=? AND res='5m' AND ts>=? AND ts<=? ORDER BY ts",
                                 (gpu_id, start, end))
        return window_stats(rows)

    def baseline(self, gpu_id: int, window_s: float | None = None) -> Window:
        now = self.clock()
        return self.window(gpu_id, now - float(window_s or self.cfg.get("tuning.baseline_window_s", 900)), now)

    # --- lifecycle -------------------------------------------------------------------------
    def start(self, uuid: str, setting: dict, hold_s: float, *, kind: str = "hourly", actor: str = "trial_manager",
              reason: str = "", run_id: str | None = None, strategy: str | None = None, step: int | None = None,
              goal: str = "max-efficiency", baseline: Window | None = None, stepped: bool = False) -> dict:
        """Apply one setting change as a trial. Returns the trial row dict, or {'error': ...} if refused."""
        gpu_id = self.db.gpu_id(uuid)
        if gpu_id is None:
            return {"error": f"unknown GPU {uuid}"}
        if self.db.open_trials(gpu_id):
            return {"error": "a trial is already open for this GPU"}
        if not self.cfg.gpu_control_enabled(uuid):
            return {"error": "control_enabled=false"}
        pre = self.actions.settings(uuid)
        base = baseline or self.baseline(gpu_id)
        if base.n == 0:
            return {"error": "no baseline samples"}
        if stepped:
            results = self.actions.apply_settings_stepped(uuid, {**pre, **setting}, actor, f"trial start: {reason}", run_id=run_id)
        else:
            results = self.actions.apply_settings(uuid, {**pre, **setting}, actor, f"trial start: {reason}", run_id=run_id,
                                                  emergency=False)
        failed = [r for r in results if not r.ok]
        if failed:
            # roll back anything that did apply, then refuse
            self.actions.apply_settings(uuid, pre, actor, "trial start failed; restoring", run_id=run_id)
            return {"error": "; ".join(r.error or r.action for r in failed)}
        if not results:
            return {"error": "setting equals current state; nothing to trial"}
        now = self.clock()
        tid = self.db.insert_trial(gpu_id, kind, setting, {"settings": pre, "window": base.as_dict(), "goal": goal},
                                   now, now + hold_s, run_id=run_id, strategy=strategy, step=step)
        log.info("trial %s started on %s: %s for %.0fs (dry_run=%s)", tid, uuid, setting, hold_s, results[0].dry_run)
        return {"id": tid, "gpu_id": gpu_id, "deadline": now + hold_s, "dry_run": results[0].dry_run}

    def judge(self, trial: dict) -> dict:
        gpu_id = trial["gpu_id"]
        uuid = self.db.one("SELECT uuid FROM gpus WHERE id=?", (gpu_id,))["uuid"]
        base = Window(**trial["baseline"]["window"])
        goal = trial["baseline"].get("goal", "max-efficiency")
        tw = self.window(gpu_id, trial["started"] + self.settle_s, trial["deadline"])
        v = compare(base, tw, goal=goal, targets=self.cfg.targets_for(uuid),
                    min_improve_pct=float(self.cfg.get("tuning.hourly.min_improve_pct", 1.0)))
        return {"verdict": v.verdict, "delta_pct": v.delta_pct, "reasons": v.reasons, "window": tw.as_dict(), "goal": goal}

    def finish(self, trial: dict, result: dict, *, actor: str = "trial_manager", run_id: str | None = None) -> str:
        """Keep or revert according to `result['verdict']`. Returns the final verdict."""
        gpu_id = trial["gpu_id"]
        uuid = self.db.one("SELECT uuid FROM gpus WHERE id=?", (gpu_id,))["uuid"]
        keep = result["verdict"] == "better"
        if trial.get("kind") == "hourly":
            self._track_convergence(gpu_id, uuid, keep, result)
        if keep:
            settings = {**trial["baseline"]["settings"], **trial["setting"]}
            self.db.save_profile(gpu_id, settings, "active", goal=result.get("goal"), score=result.get("window"),
                                 created_by=actor, note=f"trial {trial['id']} kept: {'; '.join(result['reasons'])}")
            self.db.close_trial(trial["id"], "kept", result, "; ".join(result["reasons"]), self.clock())
            log.info("trial %s kept (%s)", trial["id"], result["reasons"])
            return "kept"
        self.actions.apply_settings(uuid, trial["baseline"]["settings"], actor,
                                    f"trial {trial['id']} {result['verdict']}: revert", run_id=run_id)
        self.db.close_trial(trial["id"], "reverted", result, "; ".join(result["reasons"]), self.clock())
        log.info("trial %s reverted (%s)", trial["id"], result["reasons"])
        return "reverted"

    def revert(self, trial: dict, verdict: str, result: dict | None, reason: str, *, actor: str = "trial_manager",
               run_id: str | None = None) -> None:
        """Restore the pre-trial settings and close the trial with an explicit verdict (optimizer steps)."""
        uuid = self.db.one("SELECT uuid FROM gpus WHERE id=?", (trial["gpu_id"],))["uuid"]
        self.actions.apply_settings(uuid, trial["baseline"]["settings"], actor, f"trial {trial['id']} {verdict}: {reason}", run_id=run_id)
        self.db.close_trial(trial["id"], verdict, result, reason, self.clock())

    def _track_convergence(self, gpu_id: int, uuid: str, improved: bool, result: dict) -> None:
        """3 consecutive hourly trials without meaningful improvement -> converged for N days."""
        key = f"tune.{uuid}.noimprove"
        n = 0 if improved else int(self.db.kv_get(key, 0)) + 1
        self.db.kv_set(key, n)
        limit = int(self.cfg.get("tuning.hourly.converge_after", 3))
        if n >= limit:
            days = float(self.cfg.get("tuning.hourly.converged_days", 7))
            settings = (self.db.profile(gpu_id, "active") or self.db.profile(gpu_id, "known_good") or {}).get("settings") \
                or self.actions.settings(uuid)
            self.db.save_profile(gpu_id, settings, "converged", created_by="trial_manager",
                                 converged_until=self.clock() + days * 86400, score=result.get("window"),
                                 note=f"{n} consecutive trials without >= min improvement")
            self.db.kv_set(key, 0)
            log.info("GPU %s converged for %.0f days", uuid, days)

    def tick(self) -> list[dict]:
        """Judge every open trial whose deadline has passed. Called by the watchdog loop.
        An optimizer trial past its deadline means the optimizer died: it is always reverted, never kept."""
        done = []
        now = self.clock()
        for t in self.db.open_trials():
            if t["deadline"] <= now:
                if t["kind"] == "optimize":
                    self.revert(t, "reverted", None, "optimizer missed deadline (crashed or interrupted)")
                    done.append({"trial": t["id"], "verdict": "reverted", "reasons": ["optimizer missed deadline"]})
                    continue
                res = self.judge(t)
                verdict = self.finish(t, res)
                done.append({"trial": t["id"], "verdict": verdict, **res})
        return done

    def abort(self, trial: dict, reason: str, actor: str = "trial_manager") -> None:
        uuid = self.db.one("SELECT uuid FROM gpus WHERE id=?", (trial["gpu_id"],))["uuid"]
        try:
            self.actions.apply_settings(uuid, trial["baseline"]["settings"], actor, f"trial {trial['id']} abort: {reason}")
        except Exception:
            log.exception("abort revert failed for trial %s", trial["id"])
        self.db.close_trial(trial["id"], "aborted", None, reason, self.clock())

    def abort_all(self, reason: str, actor: str = "trial_manager", gpu_id: int | None = None) -> int:
        trials = self.db.open_trials(gpu_id)
        for t in trials:
            self.abort(t, reason, actor)
        return len(trials)

    # --- boot restore ------------------------------------------------------------------------
    def restore_profiles(self, uuids: list[str], actor: str = "boot") -> dict[str, Any]:
        """Re-apply each GPU's active profile (Windows forgets offsets on reboot). Known-good is the fallback."""
        out: dict[str, Any] = {}
        for uuid in uuids:
            gid = self.db.gpu_id(uuid)
            if gid is None or not self.cfg.gpu_control_enabled(uuid):
                out[uuid] = "skipped"
                continue
            prof = self.db.profile(gid, "active") or self.db.profile(gid, "known_good")
            if not prof:
                # first run with control enabled: the current state becomes known_good
                self.db.save_profile(gid, self.actions.settings(uuid), "known_good", created_by=actor,
                                     note="initial snapshot")
                out[uuid] = "snapshot"
                continue
            res = self.actions.apply_settings(uuid, prof["settings"], actor, f"restore {prof['status']} profile {prof['id']}")
            out[uuid] = "ok" if all(r.ok for r in res) else "partial: " + "; ".join(r.error or "" for r in res if not r.ok)
        return out
