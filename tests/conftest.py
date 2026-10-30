import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.db import Db  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    local = tmp_path / "config.local.yaml"
    local.write_text(
        f"host: {{name: testrig, role: hub, data_dir: '{tmp_path.as_posix()}/data'}}\n"
        "gpu: {backend: sim, sim: {count: 2}}\n"
        "miner: {backend: sim}\n"
        "gpus:\n  - {uuid: GPU-SIM-0000, control_enabled: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RIG_CONFIG", str(local))
    monkeypatch.delenv("RIG_WALLET_PRL", raising=False)
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def db(cfg):
    d = Db(cfg.db_path)
    yield d
    d.close()


@pytest.fixture
def stack(cfg, db):
    from agent.collector import Collector
    from agent.gpu import make_backend
    from agent.miner import make_miner
    from agent.platform import get_platform

    gpu = make_backend(cfg)
    miner = make_miner(cfg, gpu)
    plat = get_platform(cfg)
    col = Collector(cfg, db, gpu, miner, plat, pool=None)
    return cfg, db, gpu, miner, col


@pytest.fixture(autouse=True)
def _never_touch_the_host(monkeypatch):
    """Tests must never shut down / reboot the machine running them or call the network."""
    from agent.platform import linux, windows
    from agent import heartbeat
    calls = []
    for cls in (linux.LinuxPlatform, windows.WindowsPlatform):
        monkeypatch.setattr(cls, "shutdown", lambda self, reason, delay_s=30: calls.append(("shutdown", reason)))
        monkeypatch.setattr(cls, "reboot", lambda self, reason, delay_s=30: calls.append(("reboot", reason)))
    monkeypatch.setattr(heartbeat.Heartbeat, "_fetch", staticmethod(lambda url, body=None: calls.append(("ping", url))))
    return calls
