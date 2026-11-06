"""Which pool, URL, login and wallet secret a coin uses under the configured pool provider.

kryptex:    url = coins.<C>.pools.kryptex | coins.<C>.pool ; login = <RIG_WALLET_C>.<worker>
unmineable: url = coins.<C>.pools.unmineable | pool.unmineable.urls[<algo>] ;
            login = <PAYOUT>:<RIG_WALLET_PAYOUT>.<worker>[#<referral>]  (paid in PAYOUT, e.g. BTC)
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..secrets import get_secret


@dataclass
class PoolTarget:
    provider: str
    coin: str
    algo: str
    url: str | None
    wallet_secret: str          # name of the secret holding the receive address
    payout_coin: str            # coin the pool pays in
    login: str | None           # full -u value, None when the wallet secret is missing
    fee_pct: float
    note: str = ""


def pool_fee_pct(cfg: Config) -> float:
    provider = str(cfg.get("pool.provider", "kryptex"))
    if provider == "unmineable":
        ref = str(cfg.get("pool.unmineable.referral", "") or "").strip()
        return float(cfg.get("pool.unmineable.fee_pct_referral", 0.75)) if ref else float(cfg.get("pool.unmineable.fee_pct", 1.0))
    return float(cfg.get("pool.fee_pct", 1.0))


def resolve_pool(cfg: Config, coin: str, worker: str = "rig") -> PoolTarget:
    provider = str(cfg.get("pool.provider", "kryptex"))
    cat = (cfg.get("coins", {}) or {}).get(coin, {}) or {}
    algo = str(cat.get("algo", coin.lower()))
    pools = cat.get("pools", {}) or {}
    fee = pool_fee_pct(cfg)
    if provider == "unmineable":
        payout = str(cfg.get("pool.unmineable.payout_coin", "BTC")).upper()
        url = pools.get("unmineable") or (cfg.get("pool.unmineable.urls", {}) or {}).get(algo)
        secret = f"RIG_WALLET_{payout}"
        addr = get_secret(secret)
        ref = str(cfg.get("pool.unmineable.referral", "") or "").strip()
        alias = str(cfg.get("pool.unmineable.alias", "") or "").strip()
        if alias:                      # an unMineable alias stands in for COIN:ADDRESS (same balance as the alias's account)
            login = f"{alias}.{worker}" + (f"#{ref}" if ref else "")
        else:
            login = f"{payout}:{addr}.{worker}" + (f"#{ref}" if ref else "") if addr else None
        note = "" if url else f"no unMineable pool URL for {coin}/{algo} (pool.unmineable.urls or coins.{coin}.pools.unmineable)"
        return PoolTarget(provider, coin, algo, url, secret, payout, login, fee, note)
    url = pools.get("kryptex") or cat.get("pool")
    secret = f"RIG_WALLET_{coin.upper()}"
    addr = get_secret(secret)
    return PoolTarget(provider, coin, algo, url, secret, coin.upper(), f"{addr}.{worker}" if addr else None, fee,
                      "" if url else f"no pool URL for {coin}")
