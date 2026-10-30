"""SQLite storage. WAL mode, single file, versioned migrations.

Every row that concerns hardware carries host_id / gpu_id, so adding GPUs or rigs adds rows,
never columns. Workers keep the same schema locally and forward rows to the hub via `outbox`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS hosts(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, os TEXT, role TEXT, meta TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL);

CREATE TABLE IF NOT EXISTS gpus(
  id INTEGER PRIMARY KEY, host_id INTEGER NOT NULL REFERENCES hosts(id),
  uuid TEXT UNIQUE NOT NULL, idx INTEGER, model TEXT, bus_id TEXT,
  stock_pl_w REAL, min_pl_w REAL, max_pl_w REAL, vram_mb INTEGER,
  control_enabled INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, last_seen REAL NOT NULL);

-- res: '5m' (logger) or '15s' (watchdog; pruned after db.retain_15s_hours)
CREATE TABLE IF NOT EXISTS gpu_samples(
  gpu_id INTEGER NOT NULL REFERENCES gpus(id), ts REAL NOT NULL, res TEXT NOT NULL DEFAULT '5m',
  power_w REAL, core_c REAL, memj_c REAL, fan_pct REAL, fan_rpm REAL,
  core_mhz REAL, mem_mhz REAL, pl_w REAL, util_pct REAL, vram_used_mb REAL,
  core_offset_mhz REAL, mem_offset_mhz REAL,
  hashrate REAL, accepted INTEGER, rejected INTEGER, throttle TEXT, extra TEXT,
  PRIMARY KEY(gpu_id, res, ts));
CREATE INDEX IF NOT EXISTS ix_gpu_samples_res_ts ON gpu_samples(res, ts);

CREATE TABLE IF NOT EXISTS host_samples(
  host_id INTEGER NOT NULL REFERENCES hosts(id), ts REAL NOT NULL, res TEXT NOT NULL DEFAULT '5m',
  cpu_c REAL, cpu_pct REAL, cpu_w REAL, ram_used_mb REAL, ram_total_mb REAL,
  gpu_w_total REAL, wall_w REAL, miner_state TEXT, coin TEXT, miner_hashrate REAL,
  pool_hashrate REAL, pool_workers INTEGER, balance REAL, balance_usd REAL, extra TEXT,
  PRIMARY KEY(host_id, res, ts));

CREATE TABLE IF NOT EXISTS profiles(
  id INTEGER PRIMARY KEY, gpu_id INTEGER NOT NULL REFERENCES gpus(id), coin TEXT, goal TEXT,
  settings TEXT NOT NULL, score TEXT, status TEXT NOT NULL,   -- active|known_good|converged|candidate|retired
  converged_until REAL, created_by TEXT, created REAL NOT NULL, note TEXT);
CREATE INDEX IF NOT EXISTS ix_profiles_gpu ON profiles(gpu_id, status);

CREATE TABLE IF NOT EXISTS trials(
  id INTEGER PRIMARY KEY, gpu_id INTEGER NOT NULL REFERENCES gpus(id), run_id TEXT,
  kind TEXT NOT NULL,            -- hourly|optimize
  strategy TEXT, step INTEGER, setting TEXT NOT NULL, baseline TEXT, result TEXT,
  started REAL NOT NULL, deadline REAL NOT NULL, ended REAL,
  verdict TEXT,                  -- kept|reverted|aborted|open
  reason TEXT);
CREATE INDEX IF NOT EXISTS ix_trials_open ON trials(verdict, deadline);

CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, host_id INTEGER, gpu_id INTEGER,
  action TEXT NOT NULL, params TEXT, before TEXT, after TEXT, reason TEXT,
  actor TEXT NOT NULL,           -- rule|claude|user|optimizer|trial_manager|boot
  run_id TEXT, dry_run INTEGER NOT NULL, ok INTEGER NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS ix_actions_ts ON actions(ts);

CREATE TABLE IF NOT EXISTS coin_checks(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, run_id TEXT, gpu_model TEXT, coin TEXT,
  gross_usd_day REAL, net_usd_day REAL, hashrate REAL, power_w REAL, source TEXT,
  data_age_s REAL, decision TEXT, reasoning TEXT);

CREATE TABLE IF NOT EXISTS claude_runs(
  id INTEGER PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, task TEXT NOT NULL, trigger TEXT,
  schedule TEXT, started REAL NOT NULL, ended REAL, status TEXT NOT NULL,
  cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER,
  summary TEXT, output TEXT, actions_json TEXT, before_after TEXT);

CREATE TABLE IF NOT EXISTS incidents(
  id INTEGER PRIMARY KEY, opened REAL NOT NULL, closed REAL, host_id INTEGER, gpu_id INTEGER,
  rule TEXT NOT NULL, severity TEXT, ladder_step TEXT, bundle_path TEXT, claude_run_id TEXT,
  status TEXT NOT NULL, summary TEXT);

CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated REAL NOT NULL);

-- worker -> hub forwarding queue
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
"""

GPU_SAMPLE_COLS = (
    "power_w core_c memj_c fan_pct fan_rpm core_mhz mem_mhz pl_w util_pct vram_used_mb "
    "core_offset_mhz mem_offset_mhz hashrate accepted rejected throttle extra"
).split()
HOST_SAMPLE_COLS = (
    "cpu_c cpu_pct cpu_w ram_used_mb ram_total_mb gpu_w_total wall_w miner_state coin "
    "miner_hashrate pool_hashrate pool_workers balance balance_usd extra"
).split()


def _j(v: Any) -> str | None:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"), default=str)


class Db:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # --- lifecycle ------------------------------------------------------------
    def migrate(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] < SCHEMA_VERSION:
                # future: ordered ALTER statements keyed by version
                self.conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    # --- registry -------------------------------------------------------------
    def upsert_host(self, name: str, os_name: str, role: str, meta: dict | None = None) -> int:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT INTO hosts(name, os, role, meta, first_seen, last_seen) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET os=excluded.os, role=excluded.role, "
                "meta=COALESCE(excluded.meta, hosts.meta), last_seen=excluded.last_seen",
                (name, os_name, role, _j(meta), now, now),
            )
            return self.conn.execute("SELECT id FROM hosts WHERE name=?", (name,)).fetchone()["id"]

    def upsert_gpu(self, host_id: int, uuid: str, idx: int, model: str, bus_id: str | None,
                   stock_pl_w: float | None, min_pl_w: float | None, max_pl_w: float | None,
                   vram_mb: int | None, control_enabled: bool) -> int:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT INTO gpus(host_id, uuid, idx, model, bus_id, stock_pl_w, min_pl_w, max_pl_w, vram_mb, "
                "control_enabled, first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uuid) DO UPDATE SET host_id=excluded.host_id, idx=excluded.idx, model=excluded.model, "
                "bus_id=excluded.bus_id, stock_pl_w=COALESCE(excluded.stock_pl_w, gpus.stock_pl_w), "
                "min_pl_w=COALESCE(excluded.min_pl_w, gpus.min_pl_w), max_pl_w=COALESCE(excluded.max_pl_w, gpus.max_pl_w), "
                "vram_mb=COALESCE(excluded.vram_mb, gpus.vram_mb), control_enabled=excluded.control_enabled, "
                "last_seen=excluded.last_seen",
                (host_id, uuid, idx, model, bus_id, stock_pl_w, min_pl_w, max_pl_w, vram_mb,
                 int(control_enabled), now, now),
            )
            return self.conn.execute("SELECT id FROM gpus WHERE uuid=?", (uuid,)).fetchone()["id"]

    def gpu_id(self, uuid: str) -> int | None:
        row = self.one("SELECT id FROM gpus WHERE uuid=?", (uuid,))
        return row["id"] if row else None

    def gpus_for_host(self, host_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM gpus WHERE host_id=? ORDER BY idx", (host_id,))

    # --- samples --------------------------------------------------------------
    def insert_gpu_sample(self, gpu_id: int, ts: float, res: str, **cols: Any) -> None:
        vals = [_j(cols.get(c)) if c in ("throttle", "extra") else cols.get(c) for c in GPU_SAMPLE_COLS]
        with self._lock:
            self.conn.execute(
                f"INSERT OR REPLACE INTO gpu_samples(gpu_id, ts, res, {','.join(GPU_SAMPLE_COLS)}) "
                f"VALUES(?,?,?,{','.join('?' * len(GPU_SAMPLE_COLS))})",
                (gpu_id, ts, res, *vals),
            )

    def insert_host_sample(self, host_id: int, ts: float, res: str, **cols: Any) -> None:
        vals = [_j(cols.get(c)) if c == "extra" else cols.get(c) for c in HOST_SAMPLE_COLS]
        with self._lock:
            self.conn.execute(
                f"INSERT OR REPLACE INTO host_samples(host_id, ts, res, {','.join(HOST_SAMPLE_COLS)}) "
                f"VALUES(?,?,?,{','.join('?' * len(HOST_SAMPLE_COLS))})",
                (host_id, ts, res, *vals),
            )

    def recent_gpu_samples(self, gpu_id: int, since_s: float, res: str = "5m") -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM gpu_samples WHERE gpu_id=? AND res=? AND ts>=? ORDER BY ts",
            (gpu_id, res, time.time() - since_s),
        )

    def prune(self, retain_15s_hours: float, retain_5m_days: float) -> dict[str, int]:
        now = time.time()
        with self._lock:
            a = self.conn.execute("DELETE FROM gpu_samples WHERE res='15s' AND ts<?", (now - retain_15s_hours * 3600,)).rowcount
            b = self.conn.execute("DELETE FROM host_samples WHERE res='15s' AND ts<?", (now - retain_15s_hours * 3600,)).rowcount
            c = self.conn.execute("DELETE FROM gpu_samples WHERE res='5m' AND ts<?", (now - retain_5m_days * 86400,)).rowcount
            d = self.conn.execute("DELETE FROM host_samples WHERE res='5m' AND ts<?", (now - retain_5m_days * 86400,)).rowcount
        return {"gpu_15s": a, "host_15s": b, "gpu_5m": c, "host_5m": d}

    # --- actions / kv / outbox ------------------------------------------------
    def log_action(self, action: str, actor: str, ok: bool, dry_run: bool, *, host_id: int | None = None,
                   gpu_id: int | None = None, params: Any = None, before: Any = None, after: Any = None,
                   reason: str | None = None, run_id: str | None = None, error: str | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO actions(ts, host_id, gpu_id, action, params, before, after, reason, actor, run_id, "
                "dry_run, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), host_id, gpu_id, action, _j(params), _j(before), _j(after), reason, actor, run_id,
                 int(dry_run), int(ok), error),
            )
            return cur.lastrowid

    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(row["v"]) if row and row["v"] is not None else default

    def kv_set(self, k: str, v: Any) -> None:
        self.execute(
            "INSERT INTO kv(k, v, updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
            (k, json.dumps(v, default=str), time.time()),
        )

    def outbox_push(self, kind: str, payload: dict) -> None:
        self.execute("INSERT INTO outbox(created, kind, payload) VALUES(?,?,?)", (time.time(), kind, _j(payload)))

    def outbox_peek(self, limit: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,))

    def outbox_ack(self, ids: list[int]) -> None:
        if ids:
            self.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids)

    def outbox_fail(self, ids: list[int], error: str) -> None:
        if ids:
            self.execute(
                f"UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id IN ({','.join('?' * len(ids))})",
                (error[:500], *ids),
            )
