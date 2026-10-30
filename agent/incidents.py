"""Incident escalation (step 2/3): after the local ladder has acted and the owner was pushed a
notification, hand the incident to Claude in a detached headless run. The run itself is
`rigctl claude-run incident --incident <id>`; this module only decides whether/when to launch it.

Runs happen on the host where the incident occurred (rigctl acts locally). A host without Claude
configured (`claude.incident_runs: false`, e.g. a worker laptop) stays at notify-only.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from .config import REPO_ROOT, Config
from .db import Db
from .notify import Notifier

log = logging.getLogger(__name__)


class IncidentEscalator:
    def __init__(self, cfg: Config, db: Db, notifier: Notifier, clock=time.time, spawn=None):
        self.cfg, self.db, self.notifier, self.clock = cfg, db, notifier, clock
        self.spawn = spawn or self._spawn
        self.min_gap_s = float(cfg.get("claude.incident_min_gap_s", 900))

    def on_incident(self, incident_id: int) -> bool:
        """Called by the watchdog right after it opens an incident. Returns True if a Claude run was launched."""
        if not self.cfg.get("claude.incident_runs", True):
            return False
        key = f"incident.{incident_id}.claude"
        if self.db.kv_get(key):
            return False                                     # one Claude run per incident
        last = self.db.kv_get("incident.last_claude_launch", 0) or 0
        if self.clock() - last < self.min_gap_s:
            log.info("incident %s: Claude launch suppressed (last launch %.0fs ago)", incident_id, self.clock() - last)
            return False
        inc = self.db.one("SELECT rule, severity FROM incidents WHERE id=?", (incident_id,))
        if not inc:
            return False
        self.db.kv_set(key, {"launched": self.clock()})
        self.db.kv_set("incident.last_claude_launch", self.clock())
        try:
            self.spawn(incident_id)
            log.warning("incident %s (%s): Claude diagnosis launched", incident_id, inc["rule"])
            return True
        except Exception as e:
            log.exception("incident %s: launch failed", incident_id)
            self.notifier.push(f"{self.cfg.host_name}: Claude launch failed", f"incident {incident_id} {inc['rule']}: {e}",
                               priority="high", key="incident.launch_failed")
            return False

    def _spawn(self, incident_id: int) -> None:
        logs = self.cfg.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / f"incident-{incident_id}.log", "ab")
        cmd = [sys.executable, "-m", "agent.rigctl", "claude-run", "incident", "--trigger", "watchdog", "--incident", str(incident_id)]
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.DETACHED_PROCESS
        subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), creationflags=flags,
                         start_new_session=(os.name != "nt"))
