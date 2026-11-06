"""Configuration loading.

config.yaml (committed defaults) <- config.local.yaml (per-host, gitignored) <- $RIG_CONFIG.
Strings may contain ${ENV_VAR}; unresolved references are left as-is so they can be spotted.
"""
from __future__ import annotations

import copy
import os
import platform
import re
import socket
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _expand(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, list):
        return [_expand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    return obj


def default_data_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "rig-overseer"
    p = Path("/var/lib/rig-overseer")
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except PermissionError:
        return REPO_ROOT / "data"


class Config:
    def __init__(self, data: dict, sources: list[Path]):
        self.data = data
        self.sources = sources

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None else cur

    # --- host -----------------------------------------------------------------
    @property
    def host_name(self) -> str:
        return self.get("host.name") or socket.gethostname()

    @property
    def role(self) -> str:
        role = self.get("host.role", "hub")
        if role not in ("hub", "worker"):
            raise ValueError(f"host.role must be hub|worker, got {role!r}")
        return role

    @property
    def is_hub(self) -> bool:
        return self.role == "hub"

    @property
    def data_dir(self) -> Path:
        p = Path(self.get("host.data_dir") or default_data_dir())
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return Path(self.get("db.path") or (self.data_dir / "overseer.db"))

    @property
    def secrets_file(self) -> Path:
        return Path(self.get("secrets.file") or (self.data_dir / "secrets.env"))

    @property
    def dry_run(self) -> bool:
        return bool(self.get("safety.dry_run", True))

    # --- per-GPU --------------------------------------------------------------
    def gpu_entry(self, uuid: str) -> dict:
        for g in self.get("gpus", []) or []:
            if g.get("uuid") == uuid:
                return g
        return {}

    def gpu_external_oc(self, uuid: str) -> str | None:
        """Name of an external overclock controller (e.g. 'afterburner') that owns this GPU's clocks, or None."""
        v = self.gpu_entry(uuid).get("external_oc")
        return (str(v) if v not in (None, False, "") else None)

    def gpu_control_enabled(self, uuid: str) -> bool:
        return bool(self.gpu_entry(uuid).get("control_enabled", False))

    def targets_for(self, uuid: str) -> dict:
        return _deep_merge(self.get("targets", {}), self.gpu_entry(uuid).get("targets", {}))

    def clamps_for(self, model: str) -> dict:
        base = self.get("clamps.default", {})
        for needle, over in (self.get("clamps.models", {}) or {}).items():
            if needle.lower() in (model or "").lower():
                return _deep_merge(base, over)
        return copy.deepcopy(base)


def validate(cfg: "Config") -> list[str]:
    """Human-readable problems; empty list means the config is usable."""
    errs: list[str] = []
    try:
        cfg.role
    except ValueError as e:
        errs.append(str(e))
    for name, c in [("default", cfg.get("clamps.default", {}))] + list((cfg.get("clamps.models", {}) or {}).items()):
        m = _deep_merge(cfg.get("clamps.default", {}), c) if name != "default" else c
        for key in ("power_limit_pct", "core_lock_mhz", "core_offset_mhz", "mem_offset_mhz", "fan_pct"):
            v = m.get(key)
            if not (isinstance(v, list) and len(v) == 2 and v[0] <= v[1]):
                errs.append(f"clamps.{name}.{key} must be [lo, hi]")
        if m.get("power_limit_pct", [0, 0])[0] < 40:
            errs.append(f"clamps.{name}.power_limit_pct floor below 40% is unsafe for the VRM")
        if m.get("fan_pct", [0, 0])[0] < 30:
            errs.append(f"clamps.{name}.fan_pct floor below 30%")
        for key in ("power_limit_step_pct", "core_step_mhz", "mem_step_mhz", "fan_step_pct"):
            if not isinstance(m.get(key), (int, float)) or m[key] <= 0:
                errs.append(f"clamps.{name}.{key} must be > 0")
    t = cfg.get("targets", {})
    w = cfg.get("watchdog", {})
    if not (t.get("core_c_max", 0) < w.get("core_c_crit", 0) < w.get("core_c_hard", 0)):
        errs.append("targets.core_c_max < watchdog.core_c_crit < watchdog.core_c_hard required")
    if not (t.get("memj_c_max", 0) < w.get("memj_c_crit", 0) < w.get("memj_c_hard", 0)):
        errs.append("targets.memj_c_max < watchdog.memj_c_crit < watchdog.memj_c_hard required")
    for coin, c in (cfg.get("coins", {}) or {}).items():
        for k in ("algo", "miner", "pool"):
            if not c.get(k):
                errs.append(f"coins.{coin}.{k} missing")
    if cfg.get("miner.coin") not in (cfg.get("coins", {}) or {}):
        errs.append(f"miner.coin {cfg.get('miner.coin')!r} is not in the coins catalog")
    seen = set()
    for g in cfg.get("gpus", []) or []:
        if not g.get("uuid"):
            errs.append("gpus[] entry without uuid")
        elif g["uuid"] in seen:
            errs.append(f"duplicate gpus[] uuid {g['uuid']}")
        seen.add(g.get("uuid"))
    if cfg.role == "worker" and str(cfg.get("hub.url", "")).startswith("http://127.0.0.1"):
        errs.append("worker role but hub.url still points at localhost")
    if not cfg.dry_run and not any(g.get("control_enabled") for g in cfg.get("gpus", []) or []):
        errs.append("note: dry_run is off but no GPU has control_enabled=true (nothing will be controlled)")
    return errs


def load_config(path: str | os.PathLike | None = None) -> Config:
    sources: list[Path] = []
    data: dict = {}
    base = Path(path) if path else REPO_ROOT / "config.yaml"
    candidates = [base, base.with_name("config.local.yaml")]
    if os.environ.get("RIG_CONFIG"):
        candidates.append(Path(os.environ["RIG_CONFIG"]))
    for p in candidates:
        if p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                data = _deep_merge(data, yaml.safe_load(f) or {})
            sources.append(p)
    if not sources:
        raise FileNotFoundError(f"no config found at {base}")
    return Config(_expand(data), sources)
