"""Read-side queries for the dashboard. Pure functions over the hub Db; no side effects."""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Any

from agent.db import Db

ONLINE_S = 600          # a host is "online" if it sampled within 10 min
RES_COLS = ["ts", "power_w", "core_c", "memj_c", "fan_pct", "fan_rpm", "core_mhz", "mem_mhz", "pl_w", "util_pct",
            "vram_used_mb", "core_offset_mhz", "mem_offset_mhz", "hashrate", "accepted", "rejected"]


def _j(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def hosts(db: Db) -> list[dict]:
    out = []
    now = time.time()
    for h in db.query("SELECT * FROM hosts ORDER BY role DESC, name"):
        hs = db.one("SELECT * FROM host_samples WHERE host_id=? AND res='5m' ORDER BY ts DESC LIMIT 1", (h["id"],))
        d = dict(h)
        d["meta"] = _j(d.get("meta"))
        d["last_ts"] = hs["ts"] if hs else None
        d["online"] = bool(hs and now - hs["ts"] < ONLINE_S)
        d["sample"] = dict(hs) if hs else None
        if d["sample"]:
            d["sample"]["extra"] = _j(d["sample"].get("extra"))
        d["watchdog"] = db.kv_get("watchdog.state" if h["role"] == "hub" else f"watchdog.state.{h['name']}")
        d["gpu_count"] = db.one("SELECT COUNT(*) c FROM gpus WHERE host_id=?", (h["id"],))["c"]
        out.append(d)
    return out


def gpus_latest(db: Db) -> list[dict]:
    out = []
    for g in db.query("SELECT g.*, h.name host, h.role FROM gpus g JOIN hosts h ON h.id=g.host_id ORDER BY h.name, g.idx"):
        s = db.one("SELECT * FROM gpu_samples WHERE gpu_id=? ORDER BY ts DESC LIMIT 1", (g["id"],))
        d = dict(g)
        d["sample"] = dict(s) if s else None
        if d["sample"]:
            d["sample"]["throttle"] = _j(d["sample"].get("throttle")) or []
            d["sample"]["extra"] = _j(d["sample"].get("extra"))
            hr, pw = d["sample"].get("hashrate"), d["sample"].get("power_w")
            d["sample"]["eff"] = (hr / pw) if (hr and pw) else None
        for st in ("active", "converged", "known_good"):
            p = db.profile(g["id"], st)
            if p:
                d["profile"] = {"status": st, "settings": p["settings"], "created": p["created"]}
                break
        else:
            d["profile"] = None
        d["open_trial"] = bool(db.open_trials(g["id"]))
        wd = db.kv_get("watchdog.state" if g["role"] == "hub" else f"watchdog.state.{g['host']}") or {}
        d["ladder"] = (wd.get("ladder") or {}).get(g["uuid"])
        out.append(d)
    return out


def overview(db: Db) -> dict:
    hs = hosts(db)
    gs = gpus_latest(db)
    now = time.time()
    online = [h for h in hs if h["online"]]
    total_hr = sum((g["sample"] or {}).get("hashrate") or 0 for g in gs if g["sample"] and now - g["sample"]["ts"] < ONLINE_S)
    total_w = sum((g["sample"] or {}).get("power_w") or 0 for g in gs if g["sample"] and now - g["sample"]["ts"] < ONLINE_S)
    hub = next((h for h in hs if h["role"] == "hub"), hs[0] if hs else None)
    bal = {"coin": None, "amount": None, "usd": None, "source": None, "ts": None}
    if hub and hub["sample"]:
        s = hub["sample"]
        bal = {"coin": s.get("coin"), "amount": s.get("balance"), "usd": s.get("balance_usd"),
               "source": (s.get("extra") or {}).get("pool_source"), "ts": (s.get("extra") or {}).get("pool_ts"),
               "verified": (s.get("extra") or {}).get("pool_verified")}
    incidents = [dict(r) for r in db.query("SELECT * FROM incidents WHERE status='open' ORDER BY opened DESC")]
    alerts = [{"kind": "incident", "rule": i["rule"], "severity": i["severity"], "since": i["opened"],
               "summary": i["summary"], "id": i["id"]} for i in incidents]
    for h in hs:
        if not h["online"]:
            alerts.append({"kind": "offline", "rule": "host_offline", "severity": "high", "since": h["last_ts"],
                           "summary": f"{h['name']} has not reported for {int((now - (h['last_ts'] or now)) / 60)} min"})
    for g in gs:
        if g["ladder"]:
            alerts.append({"kind": "ladder", "rule": g["ladder"], "severity": "high", "since": None,
                           "summary": f"{g['host']} GPU{g['idx']} protected: {g['ladder']}"})
    uptime = None
    if hub and hub["sample"] and hub["sample"].get("extra"):
        uptime = hub["sample"]["extra"].get("uptime_s")
    last_run = db.one("SELECT task, started, status, summary FROM claude_runs ORDER BY started DESC LIMIT 1")
    return {"ts": now, "coin": (hub["sample"] or {}).get("coin") if hub else None,
            "algo": None, "total_hashrate": total_hr, "total_power_w": total_w, "uptime_s": uptime,
            "hosts_online": len(online), "hosts_total": len(hs), "gpus_total": len(gs), "balance": bal,
            "alerts": alerts, "hosts": hs, "gpus": gs,
            "last_claude_run": dict(last_run) if last_run else None}


def series(db: Db, gpu_id: int, hours: float, res: str = "5m") -> dict[str, list]:
    since = time.time() - hours * 3600
    if hours <= 48:
        rows = db.query(f"SELECT {','.join(RES_COLS)} FROM gpu_samples WHERE gpu_id=? AND res=? AND ts>=? ORDER BY ts",
                        (gpu_id, res, since))
    else:
        bucket = 3600 if hours <= 24 * 14 else 6 * 3600
        aggs = ", ".join(f"AVG({c}) {c}" for c in RES_COLS if c not in ("ts", "accepted", "rejected"))
        rows = db.query(f"SELECT CAST(ts/{bucket} AS INT)*{bucket} ts, {aggs}, MAX(accepted) accepted, MAX(rejected) rejected "
                        f"FROM gpu_samples WHERE gpu_id=? AND res='5m' AND ts>=? GROUP BY 1 ORDER BY 1", (gpu_id, since))
    cols = {c: [] for c in RES_COLS}
    cols["eff"] = []
    for r in rows:
        for c in RES_COLS:
            cols[c].append(r[c])
        hr, pw = r["hashrate"], r["power_w"]
        cols["eff"].append((hr / pw) if (hr and pw) else None)
    return cols


def log_rows(db: Db, *, host: str | None, gpu_id: int | None, since: float | None, until: float | None, res: str,
             limit: int, offset: int) -> list[dict]:
    sql = ("SELECT s.*, g.uuid, g.idx, g.model, h.name host FROM gpu_samples s JOIN gpus g ON g.id=s.gpu_id "
           "JOIN hosts h ON h.id=g.host_id WHERE s.res=?")
    params: list[Any] = [res]
    if host:
        sql += " AND h.name=?"
        params.append(host)
    if gpu_id:
        sql += " AND s.gpu_id=?"
        params.append(gpu_id)
    if since:
        sql += " AND s.ts>=?"
        params.append(since)
    if until:
        sql += " AND s.ts<=?"
        params.append(until)
    sql += " ORDER BY s.ts DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [dict(r) for r in db.query(sql, params)]


def log_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    cols = ["ts", "host", "idx", "uuid", "model"] + [c for c in RES_COLS if c != "ts"] + ["throttle"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if c == "ts" else r.get(c) for c in cols])
    return buf.getvalue()


def claude_runs(db: Db, limit: int = 50) -> list[dict]:
    out = []
    for r in db.query("SELECT id, run_id, task, trigger, schedule, started, ended, status, cost_usd, tokens_in, tokens_out, "
                      "summary FROM claude_runs ORDER BY started DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["actions"] = [dict(a) for a in db.query("SELECT ts, gpu_id, action, params, before, after, ok, dry_run, error "
                                                  "FROM actions WHERE run_id=? ORDER BY id", (r["run_id"],))]
        out.append(d)
    return out


def claude_run(db: Db, run_id: str) -> dict | None:
    r = db.one("SELECT * FROM claude_runs WHERE run_id=? OR id=?", (run_id, run_id if run_id.isdigit() else -1))
    if not r:
        return None
    d = dict(r)
    d["actions_json"], d["before_after"] = _j(d.get("actions_json")), _j(d.get("before_after"))
    d["actions"] = [dict(a) for a in db.query("SELECT * FROM actions WHERE run_id=? ORDER BY id", (d["run_id"],))]
    return d


def incidents(db: Db, limit: int = 100) -> list[dict]:
    rows = db.query("SELECT i.*, g.uuid, g.idx, h.name host FROM incidents i LEFT JOIN gpus g ON g.id=i.gpu_id "
                    "LEFT JOIN hosts h ON h.id=i.host_id ORDER BY i.opened DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def actions(db: Db, limit: int = 100, gpu_id: int | None = None) -> list[dict]:
    sql = "SELECT a.*, g.uuid, g.idx FROM actions a LEFT JOIN gpus g ON g.id=a.gpu_id"
    params: list[Any] = []
    if gpu_id:
        sql += " WHERE a.gpu_id=?"
        params.append(gpu_id)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in db.query(sql, params)]


def trials(db: Db, gpu_id: int, limit: int = 20) -> list[dict]:
    out = []
    for r in db.query("SELECT * FROM trials WHERE gpu_id=? ORDER BY id DESC LIMIT ?", (gpu_id, limit)):
        d = dict(r)
        d["setting"], d["result"] = _j(d["setting"]), _j(d["result"])
        out.append(d)
    return out
