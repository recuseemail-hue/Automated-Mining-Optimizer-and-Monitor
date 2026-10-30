from .base import PoolClient, PoolStats
from .kryptex import KryptexClient


def make_pool(cfg, db=None) -> PoolClient:
    provider = cfg.get("pool.provider", "kryptex")
    if provider == "kryptex":
        return KryptexClient(cfg, db)
    raise ValueError(f"unknown pool provider {provider!r}")


__all__ = ["PoolClient", "PoolStats", "KryptexClient", "make_pool"]
