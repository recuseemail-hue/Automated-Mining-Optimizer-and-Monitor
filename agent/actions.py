"""Allowlisted action API. The only path by which anything (rule, Claude, operator, optimizer)
changes hardware or the miner.

Every call: checks dry-run and the per-GPU control flag, enforces clamps and per-change step limits,
enforces rate limits, applies, re-reads, and writes one `actions` row (what/why/who/before/after/ok).
Clamps are never bypassed. Step limits are bypassed only for emergencies and profile restores.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import Config
from .db import Db
from .gpu import GpuBackend, GpuInfo, NotSupported
from .miner import MinerBackend
from .platform import Platform

log = logging.getLogger(__name__)

ALLOWED_ACTIONS = (
    "set_power_limit", "set_core_offset", "set_mem_offset", "set_core_lock", "reset_core_lock", "set_fan",
    "apply_settings", "miner_start", "miner_stop", "miner_restart", "miner_pause_gpu", "miner_resume_gpu",
    "switch_coin", "reboot", "shutdown",
)
SETTING_KEYS = ("pl_w", "core_offset_mhz", "mem_offset_mhz", "core_lock_mhz", "fan_pct")


class ActionError(Exception):
    pass


class ClampError(ActionError):
    pass


class RateLimited(ActionError):
    pass


@dataclass
class ActionResult:
    action: str
    ok: bool
    dry_run: bool
    params: dict
    before: Any = None
    after: Any = None
    error: str | None = None
    row_id: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Actions:
    def __init__(self, cfg: Config, db: Db, gpu: GpuBackend, miner: MinerBackend, plat: Platform, host_id: int,
                 notifier=None, clock=time.time):
        self.cfg, self.db, self.gpu, self.miner, self.plat, self.host_id = cfg, db, gpu, miner, plat, host_id
        self.notifier, self.clock = notifier, clock
        self._infos: dict[str, GpuInfo] = {}

    # --- helpers --------------------------------------------------------------------------
    def info(self, uuid: str) -> GpuInfo:
        if uuid not in self._infos:
            self._infos = {i.uuid: i for i in self.gpu.enumerate()}
        if uuid not in self._infos:
            raise ActionError(f"GPU {uuid} not present")
        return self._infos[uuid]

    def resolve(self, ref: str | int) -> str:
        """Accept a UUID or an index."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            for i in self.gpu.enumerate():
                if i.index == int(ref):
                    return i.uuid
            raise ActionError(f"no GPU with index {ref}")
        return str(ref)

    def clamps(self, uuid: str) -> dict:
        return self.cfg.clamps_for(self.info(uuid).name)

    def _dry(self, uuid: str | None, emergency: bool) -> tuple[bool, str | None]:
        """(dry_run, refusal). Refusal means the action is logged but not applied and reported as not ok."""
        if uuid is not None and not self.cfg.gpu_control_enabled(uuid):
            return True, "control_enabled=false for this GPU"
        if self.cfg.dry_run and not (emergency and self.cfg.get("safety.emergency_override_dry_run", False)):
            return True, None
        return False, None

    def current_settings(self, uuid: str) -> dict:
        s = self.gpu.sample(uuid)
        return {"pl_w": s.pl_w, "core_offset_mhz": s.core_offset_mhz, "mem_offset_mhz": s.mem_offset_mhz,
                "core_lock_mhz": s.extra.get("core_lock_mhz"), "fan_pct": s.extra.get("fan_manual_pct")}

    def _log(self, action: str, actor: str, ok: bool, dry: bool, *, uuid: str | None, params: dict, before: Any,
             after: Any, reason: str | None, run_id: str | None, error: str | None) -> ActionResult:
        gid = self.db.gpu_id(uuid) if uuid else None
        rid = self.db.log_action(action, actor, ok, dry, host_id=self.host_id, gpu_id=gid, params=params,
                                 before=before, after=after, reason=reason, run_id=run_id, error=error, ts=self.clock())
        res = ActionResult(action, ok, dry, params, before, after, error, rid)
        log.log(logging.INFO if ok else logging.WARNING, "action %s %s by %s dry=%s ok=%s %s -> %s %s",
                action, uuid or "", actor, dry, ok, before, after, error or "")
        return res

    def _bounded(self, name: str, value: float, lo: float, hi: float) -> None:
        if not lo <= value <= hi:
            raise ClampError(f"{name}={value} outside clamp [{lo}, {hi}]")

    def _step_ok(self, name: str, cur: float | None, new: float, step: float, emergency: bool) -> None:
        if cur is None or emergency:
            return
        if abs(new - cur) > step + 1e-9:
            raise ClampError(f"{name} change {cur}->{new} exceeds max step {step} per action")

    def _rate(self, action: str, limit: int, window_s: float, *, gpu_uuid: str | None = None) -> None:
        gid = self.db.gpu_id(gpu_uuid) if gpu_uuid else None
        n = self.db.count_actions_since(action, self.clock() - window_s, host_id=self.host_id, gpu_id=gid)
        if n >= limit:
            raise RateLimited(f"{action}: {n} in the last {int(window_s / 60)} min (limit {limit})")

    def _run(self, action: str, actor: str, *, uuid: str | None, params: dict, reason: str | None,
             run_id: str | None, emergency: bool, before_fn, apply_fn, after_fn, check_fn=None) -> ActionResult:
        dry, refusal = self._dry(uuid, emergency)
        before = None
        try:
            if action not in ALLOWED_ACTIONS:
                raise ActionError(f"action {action!r} not in allowlist")
            before = before_fn()
            if check_fn:
                check_fn(before)
            if refusal:
                raise ActionError(refusal)
            if dry:
                return self._log(action, actor, True, True, uuid=uuid, params=params, before=before, after=None,
                                 reason=reason, run_id=run_id, error=None)
            apply_fn()
            after = after_fn()
            return self._log(action, actor, True, False, uuid=uuid, params=params, before=before, after=after,
                             reason=reason, run_id=run_id, error=None)
        except (ActionError, NotSupported, Exception) as e:
            return self._log(action, actor, False, dry, uuid=uuid, params=params, before=before, after=None,
                             reason=reason, run_id=run_id, error=f"{type(e).__name__}: {e}"[:300])

    # --- GPU settings -----------------------------------------------------------------------
    def set_power_limit(self, ref: str | int, watts: float, actor: str, reason: str, *, run_id: str | None = None,
                        emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        info, c = self.info(uuid), self.clamps(uuid)
        stock = info.stock_pl_w or watts
        lo = stock * c["power_limit_pct"][0] / 100
        hi = min(stock * c["power_limit_pct"][1] / 100, info.max_pl_w or 1e9)
        if info.min_pl_w:
            lo = max(lo, info.min_pl_w)
        step_w = stock * c["power_limit_step_pct"] / 100

        def check(before):
            self._bounded("power_limit_w", watts, round(lo, 1), round(hi, 1))
            self._step_ok("power_limit_w", before, watts, step_w, emergency)

        return self._run("set_power_limit", actor, uuid=uuid, params={"watts": watts}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self.gpu.sample(uuid).pl_w,
                         apply_fn=lambda: self.gpu.set_power_limit_w(uuid, watts),
                         after_fn=lambda: self.gpu.sample(uuid).pl_w, check_fn=check)

    def set_core_offset(self, ref: str | int, mhz: int, actor: str, reason: str, *, run_id: str | None = None,
                        emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        c = self.clamps(uuid)

        def check(before):
            self._bounded("core_offset_mhz", mhz, *c["core_offset_mhz"])
            self._step_ok("core_offset_mhz", before, mhz, c["core_step_mhz"], emergency)

        return self._run("set_core_offset", actor, uuid=uuid, params={"mhz": mhz}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self.gpu.sample(uuid).core_offset_mhz,
                         apply_fn=lambda: self.gpu.set_core_offset_mhz(uuid, mhz),
                         after_fn=lambda: self.gpu.sample(uuid).core_offset_mhz, check_fn=check)

    def set_mem_offset(self, ref: str | int, mhz: int, actor: str, reason: str, *, run_id: str | None = None,
                       emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        c = self.clamps(uuid)

        def check(before):
            self._bounded("mem_offset_mhz", mhz, *c["mem_offset_mhz"])
            self._step_ok("mem_offset_mhz", before, mhz, c["mem_step_mhz"], emergency)

        return self._run("set_mem_offset", actor, uuid=uuid, params={"mhz": mhz}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self.gpu.sample(uuid).mem_offset_mhz,
                         apply_fn=lambda: self.gpu.set_mem_offset_mhz(uuid, mhz),
                         after_fn=lambda: self.gpu.sample(uuid).mem_offset_mhz, check_fn=check)

    def set_core_lock(self, ref: str | int, mhz: int, actor: str, reason: str, *, run_id: str | None = None,
                      emergency: bool = False) -> ActionResult:
        """Lock the core clock at `mhz` (min=max). This is the undervolt mechanism: a lower point on the VF curve."""
        uuid = self.resolve(ref)
        c = self.clamps(uuid)

        def check(before):
            self._bounded("core_lock_mhz", mhz, *c["core_lock_mhz"])
            self._step_ok("core_lock_mhz", before, mhz, c["core_step_mhz"], emergency)

        return self._run("set_core_lock", actor, uuid=uuid, params={"mhz": mhz}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self._lock_state(uuid),
                         apply_fn=lambda: self._apply_lock(uuid, mhz), after_fn=lambda: self._lock_state(uuid),
                         check_fn=check)

    def reset_core_lock(self, ref: str | int, actor: str, reason: str, *, run_id: str | None = None,
                        emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        return self._run("reset_core_lock", actor, uuid=uuid, params={}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self._lock_state(uuid),
                         apply_fn=lambda: self._apply_lock(uuid, None), after_fn=lambda: self._lock_state(uuid))

    def _lock_state(self, uuid: str) -> int | None:
        return self.db.kv_get(f"gpu.{uuid}.core_lock_mhz")

    def _apply_lock(self, uuid: str, mhz: int | None) -> None:
        if mhz is None:
            self.gpu.reset_locked_core_clock(uuid)
        else:
            self.gpu.set_locked_core_clock(uuid, mhz, mhz)
        self.db.kv_set(f"gpu.{uuid}.core_lock_mhz", mhz)   # NVML has no reliable getter for the lock

    def set_fan(self, ref: str | int, pct: int | None, actor: str, reason: str, *, run_id: str | None = None,
                emergency: bool = False) -> ActionResult:
        """pct=None returns the fan to the driver's automatic curve."""
        uuid = self.resolve(ref)
        c = self.clamps(uuid)

        def check(before):
            if pct is not None:
                self._bounded("fan_pct", pct, *c["fan_pct"])
                self._step_ok("fan_pct", before, pct, c["fan_step_pct"], emergency)

        def apply():
            if pct is None:
                self.gpu.set_fan_auto(uuid)
            else:
                self.gpu.set_fan_pct(uuid, pct)
            self.db.kv_set(f"gpu.{uuid}.fan_manual_pct", pct)

        return self._run("set_fan", actor, uuid=uuid, params={"pct": pct}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self.db.kv_get(f"gpu.{uuid}.fan_manual_pct"),
                         apply_fn=apply, after_fn=lambda: self.db.kv_get(f"gpu.{uuid}.fan_manual_pct"), check_fn=check)

    def settings(self, uuid: str) -> dict:
        """Current tunable settings as a profile dict."""
        s = self.gpu.sample(uuid)
        return {"pl_w": s.pl_w, "core_offset_mhz": s.core_offset_mhz, "mem_offset_mhz": s.mem_offset_mhz,
                "core_lock_mhz": self._lock_state(uuid), "fan_pct": self.db.kv_get(f"gpu.{uuid}.fan_manual_pct")}

    def apply_settings(self, ref: str | int, settings: dict, actor: str, reason: str, *, run_id: str | None = None,
                       emergency: bool = True) -> list[ActionResult]:
        """Apply a whole profile (restore). Clamped, but step limits are bypassed: the target is a known state."""
        uuid = self.resolve(ref)
        out: list[ActionResult] = []
        cur = self.settings(uuid)
        if settings.get("pl_w") is not None and settings["pl_w"] != cur.get("pl_w"):
            out.append(self.set_power_limit(uuid, settings["pl_w"], actor, reason, run_id=run_id, emergency=emergency))
        if "core_lock_mhz" in settings and settings["core_lock_mhz"] != cur.get("core_lock_mhz"):
            if settings["core_lock_mhz"] is None:
                out.append(self.reset_core_lock(uuid, actor, reason, run_id=run_id, emergency=emergency))
            else:
                out.append(self.set_core_lock(uuid, settings["core_lock_mhz"], actor, reason, run_id=run_id, emergency=emergency))
        if settings.get("core_offset_mhz") is not None and settings["core_offset_mhz"] != cur.get("core_offset_mhz"):
            out.append(self.set_core_offset(uuid, settings["core_offset_mhz"], actor, reason, run_id=run_id, emergency=emergency))
        if settings.get("mem_offset_mhz") is not None and settings["mem_offset_mhz"] != cur.get("mem_offset_mhz"):
            out.append(self.set_mem_offset(uuid, settings["mem_offset_mhz"], actor, reason, run_id=run_id, emergency=emergency))
        if "fan_pct" in settings and settings["fan_pct"] != cur.get("fan_pct"):
            out.append(self.set_fan(uuid, settings["fan_pct"], actor, reason, run_id=run_id, emergency=emergency))
        return out

    # --- miner ------------------------------------------------------------------------------
    def miner_start(self, actor: str, reason: str, *, run_id: str | None = None, emergency: bool = False) -> ActionResult:
        return self._run("miner_start", actor, uuid=None, params={}, reason=reason, run_id=run_id, emergency=emergency,
                         before_fn=lambda: self.miner.stats().state, apply_fn=self.miner.start,
                         after_fn=lambda: self.miner.stats().state)

    def miner_stop(self, actor: str, reason: str, *, run_id: str | None = None, emergency: bool = False) -> ActionResult:
        return self._run("miner_stop", actor, uuid=None, params={}, reason=reason, run_id=run_id, emergency=emergency,
                         before_fn=lambda: self.miner.stats().state, apply_fn=self.miner.stop,
                         after_fn=lambda: self.miner.stats().state)

    def miner_restart(self, actor: str, reason: str, *, run_id: str | None = None, emergency: bool = False) -> ActionResult:
        lim = int(self.cfg.get("limits.miner_restarts_per_30min", 3))
        return self._run("miner_restart", actor, uuid=None, params={}, reason=reason, run_id=run_id, emergency=emergency,
                         before_fn=lambda: self.miner.stats().state, apply_fn=self.miner.restart,
                         after_fn=lambda: self.miner.stats().state,
                         check_fn=lambda _b: self._rate("miner_restart", lim, 1800))

    def miner_pause_gpu(self, ref: str | int, actor: str, reason: str, *, run_id: str | None = None,
                        emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        idx = self.info(uuid).index
        return self._run("miner_pause_gpu", actor, uuid=uuid, params={"index": idx}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self._gpu_paused(idx),
                         apply_fn=lambda: self.miner.pause_gpu(idx), after_fn=lambda: self._gpu_paused(idx))

    def miner_resume_gpu(self, ref: str | int, actor: str, reason: str, *, run_id: str | None = None,
                         emergency: bool = False) -> ActionResult:
        uuid = self.resolve(ref)
        idx = self.info(uuid).index
        return self._run("miner_resume_gpu", actor, uuid=uuid, params={"index": idx}, reason=reason, run_id=run_id,
                         emergency=emergency, before_fn=lambda: self._gpu_paused(idx),
                         apply_fn=lambda: self.miner.resume_gpu(idx), after_fn=lambda: self._gpu_paused(idx))

    def _gpu_paused(self, idx: int) -> bool | None:
        g = self.miner.stats().by_index().get(idx)
        return None if g is None else g.paused

    def switch_coin(self, coin: str, actor: str, reason: str, *, run_id: str | None = None,
                    force: bool = False) -> ActionResult:
        """Validated here (catalog + daily cap); the actual miner/pool switch lands in M5 (selector)."""
        catalog = self.cfg.get("coins", {}) or {}
        lim = int(self.cfg.get("limits.coin_switches_per_day", 1))

        def check(_b):
            if coin not in catalog:
                raise ActionError(f"coin {coin!r} not in config catalog")
            if not catalog[coin].get("auto_switch", False) and actor != "user":
                raise ActionError(f"coin {coin!r} requires owner approval (auto_switch=false)")
            if not force:
                self._rate("switch_coin", lim, 86400)

        def apply():
            raise ActionError("coin switching is implemented in M5")

        return self._run("switch_coin", actor, uuid=None, params={"coin": coin}, reason=reason, run_id=run_id,
                         emergency=False, before_fn=lambda: self.cfg.get("miner.coin"), apply_fn=apply,
                         after_fn=lambda: coin, check_fn=check)

    # --- power ------------------------------------------------------------------------------
    def reboot(self, actor: str, reason: str, *, run_id: str | None = None, emergency: bool = False) -> ActionResult:
        lim = int(self.cfg.get("limits.reboots_per_day", 2))
        return self._run("reboot", actor, uuid=None, params={}, reason=reason, run_id=run_id, emergency=emergency,
                         before_fn=lambda: "up", apply_fn=lambda: self._power("reboot", reason),
                         after_fn=lambda: "rebooting", check_fn=lambda _b: self._rate("reboot", lim, 86400))

    def shutdown(self, actor: str, reason: str, *, run_id: str | None = None, emergency: bool = False) -> ActionResult:
        def check(_b):
            if not self.cfg.get("safety.allow_shutdown", True):
                raise ActionError("safety.allow_shutdown=false")
            if actor not in ("rule", "claude", "user"):
                raise ActionError("shutdown only by rule/claude/user")

        return self._run("shutdown", actor, uuid=None, params={}, reason=reason, run_id=run_id, emergency=emergency,
                         before_fn=lambda: "up", apply_fn=lambda: self._power("shutdown", reason),
                         after_fn=lambda: "shutting_down", check_fn=check)

    def _power(self, kind: str, reason: str) -> None:
        delay = int(self.cfg.get("safety.shutdown_notice_s", 45))
        if self.notifier:
            self.notifier.push(f"{self.cfg.host_name}: {kind} in {delay}s", reason, priority="urgent", tags="warning")
        getattr(self.plat, kind)(reason, delay_s=delay)
