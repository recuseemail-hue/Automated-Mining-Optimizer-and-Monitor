"""unMineable pool stats (read-only): balance in the payout coin and worker hashrates.

Endpoints follow the public v4 API used by unMineable's own app and third-party dashboards
(UNVERIFIED from the build sandbox; `rigctl pool probe` prints the raw responses):
  GET {api_base}/address/{address}?coin={PAYOUT}      -> data.balance, payment_threshold, uuid, ...
  GET {api_base}/account/{uuid}/workers               -> data.<algo>.workers[] (name, online, chr, rhr)
Responses are cached in kv; outages return the last known values with source=cache.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from ..prices import price_usd
from ..secrets import get_secret
from .base import PoolClient, PoolStats
from .kryptex import dig

log = logging.getLogger(__name__)


class UnmineableClient(PoolClient):
    name = "unmineable"

    def __init__(self, cfg, db=None):
        self.cfg, self.db = cfg, db
        self.c = cfg.get("pool.unmineable", {}) or {}
        self.payout_coin = str(self.c.get("payout_coin", "BTC")).upper()
        self.base = str(self.c.get("api_base", "https://api.unminable.com/v4")).rstrip("/")
        self.ttl = float(cfg.get("pool.cache_ttl_s", 600))
        self.verified = bool(self.c.get("verified", False))
        self.f = self.c.get("fields", {}) or {}

    def address(self) -> str | None:
        return get_secret(f"RIG_WALLET_{self.payout_coin}")

    def _get(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "rig-overseer/0.1 (+read-only stats)", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def probe(self) -> Any:
        addr = self.address()
        if not addr:
            raise RuntimeError(f"wallet secret RIG_WALLET_{self.payout_coin} not set")
        out: dict[str, Any] = {"address_endpoint": f"{self.base}/address/<addr>?coin={self.payout_coin}"}
        acct = self._get(f"{self.base}/address/{addr}?coin={self.payout_coin}")
        out["address"] = acct
        uuid = dig(acct, self.f.get("uuid", "data.uuid"))
        if uuid:
            out["workers"] = self._get(f"{self.base}/account/{uuid}/workers")
        return out

    def _cached(self) -> PoolStats | None:
        if self.db is None:
            return None
        c = self.db.kv_get("pool.cache")
        if not c or c.get("provider") != "unmineable":
            return None
        return PoolStats(ts=c["ts"], hashrate=c.get("hashrate"), workers=c.get("workers"), balance=c.get("balance"),
                         balance_usd=c.get("balance_usd"), source="cache", verified=self.verified,
                         extra={"payout_coin": self.payout_coin})

    def stats(self) -> PoolStats:
        cached = self._cached()
        if cached and time.time() - cached.ts < self.ttl:
            return cached
        try:
            raw = self.probe()
        except Exception as e:
            log.warning("unmineable stats unavailable: %s", str(e)[:120])
            if cached:
                cached.error = str(e)[:200]
                return cached
            return PoolStats(ts=time.time(), source="unavailable", verified=self.verified, error=str(e)[:200],
                             extra={"payout_coin": self.payout_coin})
        acct = raw.get("address") or {}
        balance = _num(dig(acct, self.f.get("balance", "data.balance")))
        threshold = _num(dig(acct, self.f.get("payment_threshold", "data.payment_threshold")))
        workers_raw = raw.get("workers") or {}
        algos = dig(workers_raw, self.f.get("workers_root", "data")) or {}
        n_online, hr = 0, 0.0
        for algo, block in (algos.items() if isinstance(algos, dict) else []):
            for w in (block or {}).get("workers", []) if isinstance(block, dict) else []:
                if w.get("online"):
                    n_online += 1
                hr += _num(w.get("chr")) or 0.0
        px = price_usd(self.payout_coin, self.db)
        st = PoolStats(ts=time.time(), hashrate=hr or None, workers=n_online, balance=balance,
                       balance_usd=(balance * px) if (balance is not None and px) else None, source="live",
                       verified=self.verified, raw=raw,
                       extra={"payout_coin": self.payout_coin, "payment_threshold": threshold, "price_usd": px})
        if self.db is not None:
            self.db.kv_set("pool.cache", {"provider": "unmineable", "ts": st.ts, "hashrate": st.hashrate, "workers": st.workers,
                                          "balance": st.balance, "balance_usd": st.balance_usd})
        return st


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
