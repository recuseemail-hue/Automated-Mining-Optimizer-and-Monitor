"""Context bundles for Claude runs, built by deterministic code. Nothing secret goes in here."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Config
from .scoring import window_stats


def _tail(path: Path, lines: int = 80) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except OSError:
        return ""


def gpu_table(db, host_id: int, cfg: Config | None = None) -> list[dict]:
    from .config import load_config
    cfg = cfg or load_config()
    out = []
    for g in db.gpus_for_host(host_id):
        s = db.one("SELECT * FROM gpu_samples WHERE gpu_id=? ORDER BY ts DESC LIMIT 1", (g["id"],))
        out.append({"uuid": g["uuid"], "idx": g["idx"], "model": g["model"], "control_enabled": bool(g["control_enabled"]),
                    "external_oc": cfg.gpu_external_oc(g["uuid"]), "external_profile": cfg.gpu_entry(g["uuid"]).get("external_profile"),
                    "stock_pl_w": g["stock_pl_w"], "latest": {k: s[k] for k in ("ts", "power_w", "core_c", "memj_c", "fan_pct", "pl_w",
                                                                                 "core_mhz", "mem_mhz", "core_offset_mhz", "mem_offset_mhz",
                                                                                 "hashrate", "accepted", "rejected", "throttle")} if s else None})
    return out


def build_context(cfg: Config, st: dict, task: str, *, incident_id: int | None = None) -> dict[str, Any]:
    db, col = st["db"], st["collector"]
    now = time.time()
    base = {"host": cfg.host_name, "role": cfg.role, "dry_run": cfg.dry_run, "ts": now,
            "targets": cfg.get("targets"), "clamps_default": cfg.get("clamps.default"),
            "watchdog": db.kv_get("watchdog.state"), "open_trials": db.open_trials(),
            "open_incidents": [dict(r) for r in db.query("SELECT id, rule, severity, ladder_step, opened, summary FROM incidents WHERE status='open'")]}
    coin = st["miner"].active_coin()["coin"] if hasattr(st["miner"], "active_coin") else cfg.get("miner.coin")
    if task == "hourly":
        from .tuner import Tuner
        pre = Tuner(cfg, db, st["actions"]).precheck(coin)
        return {**base, **pre, "gpu_table": gpu_table(db, col.host_id, cfg)}
    if task == "algo_check":
        from .selector import Selector
        sel = Selector(cfg, db)
        models = sorted({g["model"] for g in db.gpus_for_host(col.host_id)}) or ["RTX 3080"]
        # learn current performance for the current coin from the last 6 h
        for model in models:
            rows = []
            for g in db.gpus_for_host(col.host_id):
                if g["model"] == model:
                    rows += db.recent_gpu_samples(g["id"], 6 * 3600)
            w = window_stats(rows)
            if w.n >= 12 and w.hashrate and w.power_w:
                sel.learn_perf(model, coin, w.hashrate, w.power_w)
        d = sel.decide(models, coin)
        ms = st["miner"].stats()
        unminable = ms.state in ("error",) and "fork" in (ms.error or "").lower()
        return {**base, **d, "models": models, "catalog": cfg.get("coins"), "selector": cfg.get("selector"),
                "economics": cfg.get("economics"), "miner_reports_unminable": unminable}
    if task == "incident":
        inc = dict(db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))) if incident_id else None
        gpu_id = inc["gpu_id"] if inc else None
        since = now - 30 * 60
        tel = {}
        for g in db.gpus_for_host(col.host_id):
            if gpu_id and g["id"] != gpu_id:
                continue
            rows = db.query("SELECT ts, power_w, core_c, memj_c, fan_pct, fan_rpm, core_mhz, mem_mhz, pl_w, util_pct, hashrate, accepted, "
                            "rejected, throttle FROM gpu_samples WHERE gpu_id=? AND ts>=? ORDER BY ts", (g["id"], since))
            tel[g["uuid"]] = {"model": g["model"], "idx": g["idx"], "summary": window_stats(rows).as_dict(),
                              "samples": [dict(r) for r in rows][-120:]}
        logs = cfg.data_dir / "logs"
        return {**base, "incident": inc, "telemetry_30min": tel,
                "actions_30min": [dict(r) for r in db.query("SELECT ts, gpu_id, action, params, before, after, reason, actor, ok, dry_run, error "
                                                             "FROM actions WHERE ts>=? ORDER BY id", (since,))],
                "miner": st["miner"].stats().__dict__ | {"raw": None, "gpus": None},
                "logs": {"agent": _tail(logs / "agent.log"), "miner": _tail(logs / "peakminer.log")},
                "gpu_table": gpu_table(db, col.host_id, cfg), "system_changes_doc": "docs/system-changes.md"}
    raise ValueError(task)
