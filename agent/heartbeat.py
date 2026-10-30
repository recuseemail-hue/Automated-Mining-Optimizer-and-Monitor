"""Optional off-box heartbeat (healthchecks.io or any ping URL). The dashboard lives on the rig and
dies with it; this is how the owner learns the rig went quiet. Secret HEALTHCHECKS_URL per host.

GET <url> every `heartbeat.interval_s`; GET <url>/fail with a short body before a watchdog shutdown.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request

from .config import Config
from .secrets import get_secret

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, cfg: Config, clock=time.time, fetch=None):
        self.cfg, self.clock = cfg, clock
        self.interval = float(cfg.get("heartbeat.interval_s", 300))
        self.fetch = fetch or self._fetch
        self.last_ok: float | None = None
        self.failures = 0

    @property
    def url(self) -> str | None:
        return get_secret("HEALTHCHECKS_URL")

    @staticmethod
    def _fetch(url: str, body: bytes | None = None) -> None:
        req = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                     headers={"User-Agent": "rig-overseer/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status // 100 != 2:
                raise RuntimeError(f"heartbeat {r.status}")

    def ping(self, suffix: str = "", body: str | None = None) -> bool:
        url = self.url
        if not url:
            return False
        try:
            self.fetch(url.rstrip("/") + suffix, body.encode() if body else None)
            self.last_ok, self.failures = self.clock(), 0
            return True
        except Exception as e:
            self.failures += 1
            if self.failures in (1, 10, 100):
                log.warning("heartbeat failed (%d): %s", self.failures, str(e)[:120])
            return False

    def fail(self, reason: str) -> bool:
        return self.ping("/fail", reason[:500])

    def run_loop(self, stop: threading.Event) -> None:
        if not self.url:
            log.info("heartbeat disabled (no HEALTHCHECKS_URL)")
            return
        while not stop.is_set():
            self.ping()
            stop.wait(self.interval)
