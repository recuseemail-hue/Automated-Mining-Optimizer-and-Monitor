"""Agent service: one process per host running collector (5 min), watchdog (15 s), trial manager,
notifier retry and (workers) the hub forwarder. Boot sequence restores safety first.
"""
from __future__ import annotations

import logging
import signal
import threading
import time

from . import secrets
from .actions import Actions
from .collector import Collector
from .config import Config
from .db import Db
from .forwarder import Forwarder
from .gpu import make_backend
from .heartbeat import Heartbeat
from .incidents import IncidentEscalator
from .log import setup_logging
from .miner import make_miner
from .notify import Notifier
from .platform import get_platform
from .pool import make_pool
from .trials import TrialManager
from .watchdog import Watchdog

log = logging.getLogger(__name__)


def build(cfg: Config, *, clock=time.time, on_incident=None) -> dict:
    db = Db(cfg.db_path)
    gpu = make_backend(cfg)
    miner = make_miner(cfg, gpu)
    ov = db.kv_get("miner.coin_override")
    if ov and hasattr(miner, "coin_override"):
        miner.coin_override = ov
    plat = get_platform(cfg)
    pool = make_pool(cfg, db)
    col = Collector(cfg, db, gpu, miner, plat, pool)
    notifier = Notifier(cfg, db, clock=clock)
    heartbeat = Heartbeat(cfg, clock=clock)
    actions = Actions(cfg, db, gpu, miner, plat, col.host_id, notifier=notifier, clock=clock, heartbeat=heartbeat)
    trials = TrialManager(cfg, db, actions, clock=clock)
    escalator = IncidentEscalator(cfg, db, notifier, clock=clock)
    wd = Watchdog(cfg, db, col, actions, trials, notifier, clock=clock, on_incident=on_incident or escalator.on_incident)
    return {"db": db, "gpu": gpu, "miner": miner, "plat": plat, "pool": pool, "collector": col, "notifier": notifier,
            "actions": actions, "trials": trials, "watchdog": wd, "escalator": escalator, "heartbeat": heartbeat}


def boot_restore(stack: dict) -> dict:
    """Undo any trial left open by a crash, then re-apply each GPU's active profile."""
    trials: TrialManager = stack["trials"]
    col: Collector = stack["collector"]
    n = trials.abort_all("service boot", actor="boot")
    uuids = [i.uuid for i in col.register_gpus()]
    restored = trials.restore_profiles(uuids, actor="boot")
    stack["db"].log_action("boot_restore", "boot", True, False, host_id=col.host_id,
                           params={"aborted_trials": n, "restored": restored}, reason="service start")
    log.info("boot restore: aborted %d open trials; profiles %s", n, restored)
    return {"aborted_trials": n, "restored": restored}


def run(cfg: Config) -> None:
    secrets.configure(cfg.secrets_file)
    setup_logging(cfg.data_dir, "agent", cfg.get("logging.level", "INFO"))
    stack = build(cfg)
    boot_restore(stack)
    stop = threading.Event()

    def _sig(*_):
        stop.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    threads = [
        threading.Thread(target=stack["watchdog"].run_loop, args=(stop,), name="watchdog", daemon=True),
        threading.Thread(target=stack["collector"].run_loop, name="collector", daemon=True),
    ]
    threads.append(threading.Thread(target=stack["heartbeat"].run_loop, args=(stop,), name="heartbeat", daemon=True))
    if cfg.role == "worker":
        fw = Forwarder(cfg, stack["db"])
        threads.append(threading.Thread(target=fw.run_loop, name="forwarder", daemon=True))
    for t in threads:
        t.start()
    log.info("agent running: role=%s gpu=%s miner=%s dry_run=%s", cfg.role, stack["gpu"].name, stack["miner"].name, cfg.dry_run)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        stack["collector"].stop()
        stack["gpu"].close()
        log.info("agent stopped")
