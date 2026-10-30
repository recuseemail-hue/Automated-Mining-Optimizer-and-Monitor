from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PoolStats:
    ts: float                       # when the data was fetched (cached data keeps the original ts)
    hashrate: float | None = None   # pool-side reported H/s
    workers: int | None = None
    balance: float | None = None    # unpaid coin balance
    balance_usd: float | None = None
    source: str = "live"            # live|cache|unavailable
    verified: bool = False          # endpoint mapping confirmed against real responses
    raw: Any = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PoolClient:
    name = "base"

    def stats(self) -> PoolStats:
        raise NotImplementedError

    def probe(self) -> Any:
        """Return the raw JSON so the field mapping in config can be verified on the rig."""
        raise NotImplementedError
