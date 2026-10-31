"""Per-GPU profitability sources for the coin selector.

hashrate.no: per-GPU-model estimates (hashrate, power, revenue per coin) plus a coins list. The
endpoint paths, auth style and field names below are UNVERIFIED (docs unreachable from the build
sandbox) and are all configurable under `selector.hashrate_no`; `rigctl coins probe` prints the raw
responses so the mapping can be fixed on the rig. The free plan allows ~100 calls/month, so
responses are cached per model and a monthly call cap is enforced.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from .config import Config
from .db import Db
from .miner.base import UNIT_FACTORS
from .pool.kryptex import dig
from .secrets import get_secret

log = logging.getLogger(__name__)


def gpu_key(model: str, gpu_map: dict[str, str] | None = None) -> str | None:
    """'NVIDIA GeForce RTX 3080 Ti' -> '3080Ti' (hashrate.no style). Explicit map entries win."""
    for needle, key in (gpu_map or {}).items():
        if needle.lower() in (model or "").lower():
            return key
    m = re.search(r"(?:RTX|GTX)\s*(\d{3,4})\s*(Ti|Super|XT)?", model or "", re.I)
    if not m:
        return None
    return m.group(1) + (m.group(2).title() if m.group(2) else "")


class HashrateNoSource:
    name = "hashrate_no"

    def __init__(self, cfg: Config, db: Db, clock=time.time, fetch=None):
        self.cfg, self.db, self.clock = cfg, db, clock
        self.c = cfg.get("selector.hashrate_no", {}) or {}
        self.f = self.c.get("fields", {}) or {}
        self.ttl = float(self.c.get("cache_ttl_s", 6 * 3600))
        self.cap = int(self.c.get("monthly_call_cap", 90))
        self.fetch = fetch or self._fetch

    # --- http -------------------------------------------------------------------------------------
    def _url(self, endpoint: str, params: dict[str, Any]) -> tuple[str, dict[str, str]]:
        key = get_secret("HASHRATE_NO_KEY")
        if not key:
            raise RuntimeError("HASHRATE_NO_KEY secret not set")
        auth = self.c.get("auth", {}) or {}
        headers = {"User-Agent": "rig-overseer/0.1 (+daily profitability check)", "Accept": "application/json"}
        q = dict(params)
        if auth.get("mode", "query") == "header":
            headers[auth.get("name", "X-API-KEY")] = key
        else:
            q[auth.get("name", "apiKey")] = key
        base = str(self.c.get("base_url", "https://api.hashrate.no/v1")).rstrip("/")
        return f"{base}{endpoint}?{urllib.parse.urlencode(q)}", headers

    @staticmethod
    def _fetch(url: str, headers: dict[str, str]) -> Any:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _budget_ok(self) -> bool:
        key = "hashrate_no.calls." + time.strftime("%Y-%m", time.localtime(self.clock()))
        n = int(self.db.kv_get(key, 0))
        if n >= self.cap:
            log.warning("hashrate.no monthly call cap reached (%d/%d)", n, self.cap)
            return False
        self.db.kv_set(key, n + 1)
        return True

    def call(self, endpoint: str, params: dict[str, Any]) -> Any:
        if not self._budget_ok():
            raise RuntimeError(f"monthly call cap {self.cap} reached")
        url, headers = self._url(endpoint, params)
        return self.fetch(url, headers)

    def probe(self, model: str) -> dict[str, Any]:
        """Raw responses for the mapping check (two calls)."""
        out: dict[str, Any] = {"gpu_key": gpu_key(model, self.c.get("gpu_map"))}
        try:
            out["gpu_estimates"] = self.call(self.c.get("gpu_endpoint", "/gpuEstimates"),
                                             {self.c.get("gpu_param", "gpu"): out["gpu_key"],
                                              self.c.get("power_cost_param", "powerCost"): float(self.cfg.get("economics.kwh_usd", 0.0))})
        except Exception as e:
            out["gpu_estimates_error"] = str(e)[:300]
        if self.c.get("coins_endpoint"):
            try:
                out["coins"] = self.call(self.c["coins_endpoint"], {})
            except Exception as e:
                out["coins_error"] = str(e)[:300]
        return out

    # --- parsing ----------------------------------------------------------------------------------------
    def parse_gpu(self, raw: Any) -> dict[str, dict]:
        """-> {COIN: {hashrate (H/s), power_w, revenue_usd_day, updated}}. Tolerant to list/dict shapes."""
        f = self.f
        items = dig(raw, f.get("list")) if f.get("list") else raw
        if isinstance(items, dict) and not f.get("list"):
            # {"PRL": {...}, "XTM": {...}} or {"estimates": [...]}
            inner = items.get("estimates") or items.get("data") or items.get("coins")
            items = inner if inner is not None else [dict(v, _coin=k) for k, v in items.items() if isinstance(v, dict)]
        out: dict[str, dict] = {}
        for it in (items if isinstance(items, list) else []):
            if not isinstance(it, dict):
                continue
            coin = str(dig(it, f.get("coin", "coin")) or it.get("_coin") or "").upper().strip()
            if not coin:
                continue
            hr = dig(it, f.get("hashrate", "hashrate"))
            unit = str(dig(it, f.get("hashrate_unit", "hashrateUnit")) or f.get("default_unit", "H/s")).lower().replace("/s", "") + "/s"
            try:
                hr_hs = float(hr) * UNIT_FACTORS.get(unit, 1.0) if hr is not None else None
            except (TypeError, ValueError):
                hr_hs = None
            rev = dig(it, f.get("revenue_usd_day", "revenueUSD"))
            if rev is None:
                rev = dig(it, f.get("profit_usd_day", "profitUSD"))
            pw = dig(it, f.get("power_w", "power"))
            upd = dig(it, f.get("updated", "updated"))
            try:
                upd_ts = float(upd) if isinstance(upd, (int, float)) else (time.mktime(time.strptime(str(upd)[:19], "%Y-%m-%dT%H:%M:%S")) if upd else None)
            except ValueError:
                upd_ts = None
            try:
                out[coin] = {"hashrate": hr_hs, "power_w": float(pw) if pw is not None else None,
                             "revenue_usd_day": float(rev) if rev is not None else None, "updated": upd_ts,
                             "algo": dig(it, f.get("algo", "algorithm"))}
            except (TypeError, ValueError):
                continue
        return out

    # --- cached estimates per model ------------------------------------------------------------------------
    def estimates(self, model: str) -> tuple[dict[str, dict], dict]:
        """Per-coin estimates for a GPU model, cached in kv. meta says live/cache/unavailable + age."""
        key = f"hashrate_no.cache.{gpu_key(model, self.c.get('gpu_map')) or model}"
        cached = self.db.kv_get(key)
        now = self.clock()
        if cached and now - cached["ts"] < self.ttl:
            return cached["data"], {"source": "hashrate_no(cache)", "fetched": cached["ts"], "age_s": now - cached["ts"]}
        gk = gpu_key(model, self.c.get("gpu_map"))
        if not gk:
            return {}, {"source": "unavailable", "error": f"no hashrate.no GPU key for model {model!r} (selector.hashrate_no.gpu_map)"}
        try:
            raw = self.call(self.c.get("gpu_endpoint", "/gpuEstimates"),
                            {self.c.get("gpu_param", "gpu"): gk,
                             self.c.get("power_cost_param", "powerCost"): float(self.cfg.get("economics.kwh_usd", 0.0))})
            data = self.parse_gpu(raw)
            if not data:
                raise RuntimeError("no coins parsed from response; check selector.hashrate_no.fields with `rigctl coins probe`")
            self.db.kv_set(key, {"ts": now, "data": data})
            return data, {"source": "hashrate_no", "fetched": now, "age_s": 0.0}
        except Exception as e:
            log.warning("hashrate.no %s: %s", model, str(e)[:160])
            if cached:
                return cached["data"], {"source": "hashrate_no(cache)", "fetched": cached["ts"], "age_s": now - cached["ts"],
                                        "error": str(e)[:200]}
            return {}, {"source": "unavailable", "error": str(e)[:200]}
