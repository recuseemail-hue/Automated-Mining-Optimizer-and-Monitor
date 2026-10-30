"""Phone notifications via ntfy (topic and optional token are secrets).

Sends are best-effort and never block protection: on failure the message is queued in `outbox`
(kind 'notify') and retried by the agent service. Identical alert keys are rate-limited.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

from .config import Config
from .db import Db
from .secrets import get_secret

log = logging.getLogger(__name__)

PRIORITY = {"min": "1", "low": "2", "default": "3", "high": "4", "urgent": "5"}


class Notifier:
    def __init__(self, cfg: Config, db: Db, clock=time.time):
        self.cfg, self.db, self.clock = cfg, db, clock
        self.provider = cfg.get("notify.provider", "ntfy")
        self.cooldown = float(cfg.get("notify.cooldown_s", 600))
        self.sent: list[dict] = []          # in-memory record (tests, dashboard "last alerts")

    def _allowed(self, key: str | None) -> bool:
        if not key:
            return True
        last = self.db.kv_get(f"notify.last.{key}")
        if last and self.clock() - last < self.cooldown:
            return False
        self.db.kv_set(f"notify.last.{key}", self.clock())
        return True

    def push(self, title: str, message: str, *, priority: str = "default", tags: str | None = None,
             key: str | None = None) -> bool:
        """Returns True if delivered now; False if suppressed or queued for retry."""
        if not self._allowed(key):
            return False
        payload = {"title": title, "message": message, "priority": priority, "tags": tags, "ts": self.clock()}
        self.sent.append(payload)
        if self.provider == "none":
            return True
        try:
            self._send(payload)
            return True
        except Exception as e:
            log.warning("notify failed, queued: %s", str(e)[:120])
            self.db.outbox_push("notify", payload)
            return False

    def _send(self, p: dict) -> None:
        if self.provider != "ntfy":
            raise RuntimeError(f"unknown notify provider {self.provider}")
        topic = get_secret("NTFY_TOPIC")
        if not topic:
            raise RuntimeError("NTFY_TOPIC secret not set")
        url = str(self.cfg.get("notify.ntfy.url", "https://ntfy.sh")).rstrip("/") + "/" + topic
        headers = {"Title": p["title"].encode("ascii", "replace").decode(), "Priority": PRIORITY.get(p["priority"], "3")}
        if p.get("tags"):
            headers["Tags"] = p["tags"]
        tok = get_secret("NTFY_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, data=p["message"].encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status // 100 != 2:
                raise RuntimeError(f"ntfy {r.status}")

    def flush(self, limit: int = 20) -> int:
        """Retry queued notifications. Called periodically by the service."""
        n = 0
        for row in self.db.outbox_peek(limit, kinds=("notify",)):
            p = json.loads(row["payload"])
            if self.clock() - p.get("ts", 0) > 6 * 3600:      # stale alerts are dropped, not replayed
                self.db.outbox_ack([row["id"]])
                continue
            try:
                self._send(p)
                self.db.outbox_ack([row["id"]])
                n += 1
            except Exception as e:
                self.db.outbox_fail([row["id"]], str(e))
                break
        return n
