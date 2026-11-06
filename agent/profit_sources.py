"""Per-GPU profitability sources for the coin selector.

hashrate.no v1 API (shape taken from a working third-party client; confirm with `rigctl coins probe`):
  GET https://api.hashrate.no/v1/coins?apiKey=KEY
      -> [{coin, name, algo, usdPrice, coinEstimate, coinEstimateUnit, usdEstimate, usdEstimate24h, networkHashrate, ...}]
         usdEstimate = USD/day for `coinEstimateUnit` of hashrate (e.g. "1 MH/s"); strings, not numbers.
  GET https://api.hashrate.no/v1/gpuEstimates?apiKey=KEY&powerCost=0.0
      -> {"<gpu key>": {device:{name,brand}, profit:{coin,revenueUSD,profitUSD}, revenue:{...}, profit24:{...}, revenue24:{...}}}
         (best coin per GPU only; used as a cross-check and for the Claude context, not as the per-coin table)
Per-coin revenue for a GPU model = usdEstimate / unit_hashrate * expected_hashrate(model, coin), where the expected
hashrate comes from the rig's own measurements (learned) or the seed table. Free plan ~100 calls/month, so both
responses are cached (6 h) and a monthly call cap is enforced.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import Config
from .db import Db
from .miner.base import UNIT_FACTORS
from .secrets import get_secret

log = logging.getLogger(__name__)
_UNIT_RE = re.compile(r"^\s*([\d.,]*)\s*([kKmMgGtTpP]?)[hH](?:/s)?\s*$")


def gpu_key(model: str, gpu_map: dict[str, str] | None = None) -> str | None:
    """'NVIDIA GeForce RTX 3080 Ti' -> '3080Ti'. Used to find the model in gpuEstimates; explicit map entries win."""
    for needle, key in (gpu_map or {}).items():
        if needle.lower() in (model or "").lower():
            return key
    m = re.search(r"(?:RTX|GTX)\s*(\d{3,4})\s*(Ti|Super|XT)?", model or "", re.I)
    if not m:
        return None
    return m.group(1) + (m.group(2).title() if m.group(2) else "")


def parse_unit_hs(unit: str | None) -> float | None:
    """'1 MH/s' -> 1e6, 'kH/s' -> 1e3, '100 GH/s' -> 1e11. None when unparsable."""
    if not unit:
        return None
    m = _UNIT_RE.match(str(unit))
    if not m:
        return None
    n = float(m.group(1).replace(",", "")) if m.group(1) else 1.0
    return n * UNIT_FACTORS.get((m.group(2).lower() + "h/s") if m.group(2) else "h/s", 1.0)


def _num(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


class HashrateNoSource:
    name = "hashrate_no"

    def __init__(self, cfg: Config, db: Db, clock=time.time, fetch=None):
        self.cfg, self.db, self.clock = cfg, db, clock
        self.c = cfg.get("selector.hashrate_no", {}) or {}
        self.ttl = float(self.c.get("cache_ttl_s", 6 * 3600))
        self.cap = int(self.c.get("monthly_call_cap", 90))
        self.fetch = fetch or self._fetch

    # --- http -------------------------------------------------------------------------------------
    def _url(self, endpoint: str, params: dict[str, Any]) -> str:
        key = get_secret("HASHRATE_NO_KEY")
        if not key:
            raise RuntimeError("HASHRATE_NO_KEY secret not set")
        base = str(self.c.get("base_url", "https://api.hashrate.no/v1")).rstrip("/")
        return f"{base}/{endpoint.lstrip('/')}?{urllib.parse.urlencode({'apiKey': key, **params})}"

    @staticmethod
    def _fetch(url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "rig-overseer/0.1 (+daily profitability check)",
                                                   "Accept": "application/json"})
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

    def _cached_call(self, endpoint: str, params: dict[str, Any], cache_key: str) -> tuple[Any, dict]:
        cached = self.db.kv_get(cache_key)
        now = self.clock()
        if cached and now - cached["ts"] < self.ttl:
            return cached["data"], {"source": "hashrate_no(cache)", "fetched": cached["ts"], "age_s": now - cached["ts"]}
        try:
            if not self._budget_ok():
                raise RuntimeError(f"monthly call cap {self.cap} reached")
            data = self.fetch(self._url(endpoint, params))
            if isinstance(data, dict) and "title" in data and "detail" in data:      # API error envelope
                raise RuntimeError(f"{data.get('title')}: {data.get('detail')}")
            self.db.kv_set(cache_key, {"ts": now, "data": data})
            return data, {"source": "hashrate_no", "fetched": now, "age_s": 0.0}
        except Exception as e:
            log.warning("hashrate.no %s: %s", endpoint, str(e)[:160])
            if cached:
                return cached["data"], {"source": "hashrate_no(cache)", "fetched": cached["ts"], "age_s": now - cached["ts"],
                                        "error": str(e)[:200]}
            return None, {"source": "unavailable", "error": str(e)[:200]}

    def coins(self) -> tuple[dict[str, dict], dict]:
        """{SYMBOL: {usd_per_hs, usd_price, algo, name, unit, usd_estimate, network_hashrate}} from /coins."""
        raw, meta = self._cached_call(self.c.get("coins_endpoint", "coins"), {}, "hashrate_no.cache.coins")
        out: dict[str, dict] = {}
        for row in (raw if isinstance(raw, list) else []):
            if not isinstance(row, dict):
                continue
            sym = str(row.get("coin") or "").upper()
            unit_hs = parse_unit_hs(row.get("coinEstimateUnit"))
            usd = _num(row.get("usdEstimate"))
            if not sym:
                continue
            out[sym] = {"usd_per_hs": (usd / unit_hs) if (usd is not None and unit_hs) else None, "usd_estimate": usd,
                        "unit": row.get("coinEstimateUnit"), "usd_price": _num(row.get("usdPrice")), "algo": row.get("algo"),
                        "name": row.get("name"), "network_hashrate": row.get("networkHashrate"),
                        "usd_estimate_24h": _num(row.get("usdEstimate24h"))}
        if raw is not None and not out:
            meta["error"] = "coins response parsed to nothing; check `rigctl coins probe`"
        return out, meta

    def gpu_best(self, model: str) -> tuple[dict | None, dict]:
        """hashrate.no's own 'best coin' line for this GPU model (cross-check), from /gpuEstimates."""
        if not self.c.get("fetch_gpu_estimates", True):
            return None, {"source": "disabled"}
        raw, meta = self._cached_call(self.c.get("gpu_endpoint", "gpuEstimates"),
                                      {"powerCost": float(self.cfg.get("economics.kwh_usd", 0.0))}, "hashrate_no.cache.gpu")
        if not isinstance(raw, dict):
            return None, meta
        key = gpu_key(model, self.c.get("gpu_map"))
        for k, v in raw.items():
            name = str((v or {}).get("device", {}).get("name", ""))
            if (key and k.lower() == key.lower()) or (key and gpu_key(name) == key) or name.lower() in (model or "").lower():
                return {"key": k, **(v or {})}, meta
        return None, meta

    def probe(self, model: str) -> dict[str, Any]:
        coins, m1 = self.coins()
        best, m2 = self.gpu_best(model)
        sample = {k: coins[k] for k in list(coins)[:3]}
        wanted = {k: coins.get(self.symbol_for(k)) for k in (self.cfg.get("coins", {}) or {})}
        return {"coins_meta": m1, "coins_count": len(coins), "sample": sample, "catalog_coins": wanted,
                "gpu_key": gpu_key(model, self.c.get("gpu_map")), "gpu_best": best, "gpu_meta": m2}

    def symbol_for(self, coin: str) -> str:
        cat = (self.cfg.get("coins", {}) or {}).get(coin, {}) or {}
        return str(cat.get("hashrate_no_symbol") or coin).upper()

    # --- per-model estimates -------------------------------------------------------------------------
    def estimates(self, model: str, perf_lookup: Callable[[str, str], dict | None] | None = None) -> tuple[dict[str, dict], dict]:
        """{COIN: {hashrate, power_w, revenue_usd_day, usd_per_hs, updated}} for the catalog coins. The expected
        hashrate per coin comes from perf_lookup(model, coin) (learned telemetry or the seed table)."""
        coins, meta = self.coins()
        out: dict[str, dict] = {}
        for coin in (self.cfg.get("coins", {}) or {}):
            row = coins.get(self.symbol_for(coin))
            if not row or row.get("usd_per_hs") is None:
                continue
            perf = perf_lookup(model, coin) if perf_lookup else None
            hr = float(perf["hashrate"]) if perf and perf.get("hashrate") else None
            out[coin] = {"hashrate": hr, "power_w": float(perf["power_w"]) if perf and perf.get("power_w") else None,
                         "revenue_usd_day": (row["usd_per_hs"] * hr) if hr else None, "usd_per_hs": row["usd_per_hs"],
                         "usd_price": row.get("usd_price"), "algo": row.get("algo"), "updated": meta.get("fetched")}
        return out, meta
