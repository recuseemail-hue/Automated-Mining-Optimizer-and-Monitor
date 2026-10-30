"""Kryptex pool stats (read-only).

The endpoint and field names in config.yaml are UNVERIFIED (docs were unreachable from the build
sandbox). Responses are cached in kv so an outage or API change never blanks the dashboard; the
PoolStats.source field says whether a value is live or cached. Nothing here can move funds: the
only input is the public wallet address.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from ..secrets import wallet_for
from .base import PoolClient, PoolStats

log = logging.getLogger(__name__)


def dig(obj: Any, path: str | None) -> Any:
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


class KryptexClient(PoolClient):
    name = "kryptex"

    def __init__(self, cfg, db=None):
        self.cfg, self.db = cfg, db
        self.ttl = float(cfg.get("pool.cache_ttl_s", 600))
        self.verified = bool(cfg.get("pool.verified", False))

    def url(self) -> str | None:
        coin = self.cfg.get("miner.coin", "PRL")
        wallet = wallet_for(coin)
        if not wallet:
            return None
        return str(self.cfg.get("pool.stats_url")).replace("${WALLET}", wallet).replace("${COIN}", coin.lower())

    def probe(self) -> Any:
        url = self.url()
        if not url:
            raise RuntimeError("wallet secret RIG_WALLET_<COIN> not set")
        req = urllib.request.Request(url, headers={"User-Agent": "rig-overseer/0.1 (+read-only stats)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _cached(self) -> PoolStats | None:
        if self.db is None:
            return None
        c = self.db.kv_get("pool.cache")
        if not c:
            return None
        return PoolStats(ts=c["ts"], hashrate=c.get("hashrate"), workers=c.get("workers"), balance=c.get("balance"),
                         balance_usd=c.get("balance_usd"), source="cache", verified=self.verified)

    def stats(self) -> PoolStats:
        cached = self._cached()
        if cached and time.time() - cached.ts < self.ttl:
            return cached
        try:
            raw = self.probe()
        except Exception as e:
            log.warning("pool stats unavailable: %s", str(e)[:120])
            if cached:
                cached.error = str(e)[:200]
                return cached
            return PoolStats(ts=time.time(), source="unavailable", verified=self.verified, error=str(e)[:200])
        f = self.cfg.get("pool.fields", {}) or {}

        def num(path):
            v = dig(raw, f.get(path))
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None

        st = PoolStats(ts=time.time(), hashrate=num("hashrate"), workers=None if num("workers") is None else int(num("workers")),
                       balance=num("balance"), balance_usd=num("balance_usd"), source="live", verified=self.verified, raw=raw)
        if self.db is not None:
            self.db.kv_set("pool.cache", {"ts": st.ts, "hashrate": st.hashrate, "workers": st.workers,
                                          "balance": st.balance, "balance_usd": st.balance_usd})
        return st
