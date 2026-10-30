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
