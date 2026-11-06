"""USD prices for payout coins (CoinGecko simple-price, no key). Cached in kv for 10 minutes;
on failure the last cached price is used so the dashboard never blanks."""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "USDT": "tether", "USDC": "usd-coin", "DOGE": "dogecoin",
                 "LTC": "litecoin", "SOL": "solana", "XMR": "monero", "TRX": "tron", "BNB": "binancecoin",
                 "XRP": "ripple", "ADA": "cardano", "BCH": "bitcoin-cash", "SHIB": "shiba-inu"}
TTL_S = 600


def price_usd(symbol: str, db=None, fetch=None) -> float | None:
    sym = symbol.upper()
    if sym in ("USDT", "USDC"):
        return 1.0
    key = f"price.{sym}"
    cached = db.kv_get(key) if db is not None else None
    now = time.time()
    if cached and now - cached["ts"] < TTL_S:
        return cached["usd"]
    cg = COINGECKO_IDS.get(sym)
    if not cg:
        return cached["usd"] if cached else None
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?" + urllib.parse.urlencode({"ids": cg, "vs_currencies": "usd"})
        raw = (fetch or _fetch)(url)
        usd = float(raw[cg]["usd"])
        if db is not None:
            db.kv_set(key, {"ts": now, "usd": usd})
        return usd
    except Exception as e:
        log.warning("price %s unavailable: %s", sym, str(e)[:100])
        return cached["usd"] if cached else None


def _fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "rig-overseer/0.1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))
