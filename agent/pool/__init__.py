from .base import PoolClient, PoolStats
from .kryptex import KryptexClient
from .resolve import PoolTarget, pool_fee_pct, resolve_pool
from .unmineable import UnmineableClient


def make_pool(cfg, db=None) -> PoolClient:
    provider = cfg.get("pool.provider", "kryptex")
    if provider == "kryptex":
        return KryptexClient(cfg, db)
    if provider == "unmineable":
        return UnmineableClient(cfg, db)
    raise ValueError(f"unknown pool provider {provider!r}")


__all__ = ["PoolClient", "PoolStats", "PoolTarget", "KryptexClient", "UnmineableClient", "make_pool", "resolve_pool", "pool_fee_pct"]
