"""Read-only SRBMiner backend, AutoMiner switching, unMineable alias login."""
import pytest

from agent.miner.base import MinerStats
from agent.miner.srbminer import SRBMiner, AutoMiner, parse_stats
from agent.pool.resolve import resolve_pool

RAW = {"miner_version": "2.9.5", "mining_time": 3600, "rig_name": "RIG",
       "algorithms": [{"name": "pearl", "hashrate": {"gpu": {"total": 125.3e12, "gpu0": 125.3e12}},
                       "shares": {"accepted": 42, "rejected": 1}, "pool": {"pool": "stratum+tcp://pearlpow.unmineable.com:3333"}}],
       "gpu_devices": [{"id": 0, "bus_id": 5, "model": "NVIDIA GeForce RTX 3080", "temperature": 70, "fan_speed": 66,
                        "core_clock": 1890, "memory_clock": 9251}]}


def test_parse_stats():
    st = parse_stats(RAW)
    assert st.state == "running" and st.coin == "PRL" and st.algo == "pearl" and st.total_hashrate == 125.3e12
    g = st.gpus[0]
    assert g.bus_id == "05:00.0" and g.hashrate == 125.3e12 and g.accepted == 42 and g.rejected == 1 and g.temp_c == 70
    assert st.by_bus()["05:00.0"] is g and st.extra["read_only"]
    assert parse_stats([]).state == "error"


class FakeProc:
    def __init__(self, cmdline):
        self.info = {"name": "SRBMiner-MULTI.exe", "cmdline": cmdline}
        self.pid = 1


def test_port_detection_and_stats(cfg, monkeypatch):
    cfg.data["miner"]["backend"] = "srbminer"
    s = SRBMiner(cfg)
    monkeypatch.setattr(SRBMiner, "process", lambda self: FakeProc(["SRBMiner-MULTI.exe", "--algorithm-gpu", "pearl", "--api-enable", "--api-port", "60130"]))
    assert s.detect_port() == 60130
    monkeypatch.setattr(SRBMiner, "fetch_raw", lambda self: RAW)
    assert s.stats().total_hashrate == 125.3e12 and s.is_running()
    monkeypatch.setattr(SRBMiner, "process", lambda self: FakeProc(["SRBMiner-MULTI.exe", "--api-enable"]))
    assert s.detect_port() == 21550                                      # config fallback
    monkeypatch.setattr(SRBMiner, "process", lambda self: None)
    assert s.stats().state == "stopped"
    with pytest.raises(NotImplementedError):
        s.start()


class StubPeak:
    supports_oc_api = True
    coin_override = None

    def __init__(self, state):
        self.state = state
        self.started = 0

    def stats(self):
        return MinerStats(state=self.state, coin="PRL", total_hashrate=100.0 if self.state == "running" else 0)

    def active_coin(self):
        return {"coin": "PRL", "algo": "pearl", "pool": "x", "dev_fee_pct": 2.0}

    def is_running(self):
        return self.state == "running"

    def start(self):
        self.started += 1
        self.state = "running"

    def stop(self):
        self.state = "stopped"

    def oc_get(self, i):
        return {"id": i}


def test_auto_miner_prefers_peakminer_then_reads_srbminer(cfg, monkeypatch):
    peak = StubPeak("unreachable")
    srb = SRBMiner(cfg)
    monkeypatch.setattr(SRBMiner, "process", lambda self: FakeProc(["SRBMiner-MULTI.exe", "--api-port", "60130"]))
    monkeypatch.setattr(SRBMiner, "fetch_raw", lambda self: RAW)
    auto = AutoMiner(cfg, peak, srb)
    st = auto.stats()
    assert st.total_hashrate == 125.3e12 and auto.current is srb and not auto.supports_oc_api
    with pytest.raises(RuntimeError, match="close it"):
        auto.start()                                                     # never two miners on the card
    monkeypatch.setattr(SRBMiner, "process", lambda self: None)
    peak.state = "running"
    assert auto.stats().total_hashrate == 100.0 and auto.current is peak and auto.supports_oc_api
    assert auto.oc_get(0) == {"id": 0}                                   # control calls reach PeakMiner


def test_unmineable_alias_login(cfg, monkeypatch):
    cfg.data["pool"]["provider"] = "unmineable"
    cfg.data["pool"]["unmineable"]["alias"] = "Killrang"
    monkeypatch.delenv("RIG_WALLET_BTC", raising=False)
    t = resolve_pool(cfg, "PRL", "rig1")
    assert t.login == "Killrang.rig1" and t.url == "pearlpow.unmineable.com:3333"
    cfg.data["pool"]["unmineable"]["referral"] = "ab12"
    assert resolve_pool(cfg, "PRL", "rig1").login == "Killrang.rig1#ab12"
