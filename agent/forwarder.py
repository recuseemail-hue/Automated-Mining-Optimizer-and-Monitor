"""Worker -> hub forwarding of queued rows (outbox). Hub ingest endpoint lands in M3 (/api).

Runs inside the agent service on workers only. Network failure leaves rows queued; nothing is lost
and nothing local depends on the hub being reachable.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from .config import Config
from .db import Db
from .secrets import get_secret

log = logging.getLogger(__name__)


class Forwarder:
    def __init__(self, cfg: Config, db: Db):
        self.cfg, self.db = cfg, db
        self.url = str(cfg.get("hub.url")).rstrip("/") + "/api/ingest"
        self.batch = int(cfg.get("hub.forward_batch", 200))
        self.interval = float(cfg.get("hub.forward_interval_s", 60))
        self._stop = threading.Event()

    def flush_once(self) -> int:
        rows = self.db.outbox_peek(self.batch)
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        body = json.dumps({"host": self.cfg.host_name,
                           "items": [{"kind": r["kind"], "payload": json.loads(r["payload"])} for r in rows]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {get_secret('RIG_HUB_TOKEN', '')}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status // 100 != 2:
                    raise RuntimeError(f"hub returned {r.status}")
        except Exception as e:
            self.db.outbox_fail(ids, str(e))
            log.warning("forward failed (%d rows queued): %s", len(ids), str(e)[:120])
            return 0
        self.db.outbox_ack(ids)
        return len(ids)

    def run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                while self.flush_once() == self.batch:
                    pass
            except Exception:
                log.exception("forwarder error")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
