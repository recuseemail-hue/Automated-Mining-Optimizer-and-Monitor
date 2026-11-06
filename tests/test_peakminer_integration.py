"""PeakMiner command line, control API, the NVML->miner write router, and managed-miner lifecycle."""
import json

import pytest

from agent import service
from agent.gpu.base import NotSupported
from agent.gpu.router import ControlRouter
from agent.miner.peakminer import PeakMiner


class FakeHTTP:
    """Scripted responses for PeakMiner._req: (method, path) -> (code, body)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        r = self.routes.get((method, path))
        if callable(r):
            r = r(body)
        return r if r is not None else (404, {"error": "off"})


@pytest.fixture
def pm(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_WALLET_PRL", "prl1wallet")
    monkeypatch.setenv("PEAKMINER_API_KEY", "k-123456")
    cfg.data["miner"]["exe"] = str(tmp_path / "peakminer.exe")
    cfg.data["host"]["name"] = "Rig 1!"
    return PeakMiner(cfg)


def test_command_line_matches_docs(pm, cfg, monkeypatch):
    cmd = pm.command()
    assert cmd[1:7] == ["--coin", "pearl", "-o", "prl.kryptex.network:7048", "-u", "prl1wallet.Rig1"]
    assert "-a" in cmd and cmd[cmd.index("-a") + 1] == "127.0.0.1:4068"
    assert cmd[cmd.index("--api-key") + 1] == "k-123456" and cmd[cmd.index("--api-control") + 1] == "pause,oc"
    assert cmd[cmd.index("--gpu-temp-stop") + 1] == "85" and cmd[cmd.index("--gpu-temp-start") + 1] == "72"
    assert "--log-append" in cmd and "--no-color" in cmd and "--state-dir" in cmd
    monkeypatch.setenv("RIG_WALLET_QTC", "qzwallet")
    pm.coin_override = "QTC"
    cmd = pm.command()
    assert cmd[cmd.index("--coin") + 1] == "quantus" and "qtc.kryptex.network:7049" in cmd[cmd.index("-o") + 1]
    assert cmd[cmd.index("-u") + 1] == "qzwallet.Rig1"


def test_restart_refuses_without_wallet_and_401_is_not_down(cfg, monkeypatch, tmp_path):
    monkeypatch.delenv("RIG_WALLET_PRL", raising=False)
    monkeypatch.delenv("RIG_WALLET_BTC", raising=False)
    monkeypatch.setenv("PEAKMINER_API_KEY", "k")
    cfg.data["miner"]["exe"] = str(tmp_path / "peakminer.exe")
    p = PeakMiner(cfg)
    stopped = []
    p.stop = lambda: stopped.append(1)
    with pytest.raises(RuntimeError, match="payout address"):
        p.restart()
    assert stopped == []                                                 # a healthy miner is never killed first
    p._req = lambda method, path, body=None, timeout=None: (401, {"error": "unauthorized"})
    st = p.stats()
    assert st.state == "unauthorized" and "key" in st.error


def test_api_key_generated_when_missing(cfg, monkeypatch, tmp_path):
    from agent import secrets as sec
    monkeypatch.delenv("PEAKMINER_API_KEY", raising=False)
    sec.configure(tmp_path / "secrets.env")
    monkeypatch.setenv("RIG_WALLET_PRL", "w")
    p = PeakMiner(cfg)
    key = p.api_key()
    assert len(key) > 20 and "PEAKMINER_API_KEY=" in (tmp_path / "secrets.env").read_text()


def test_stats_pause_and_oc_via_control_api(pm):
    summary = json.loads(open("tests/fixtures/peakminer_summary.json").read())
    http = FakeHTTP({
        ("GET", "/summary"): (200, summary),
        ("GET", "/health"): (200, {"ok": True, "version": "2.20.5"}),
        ("POST", "/control/pause"): lambda b: (200, [{"id": b["gpus"][0], "paused": True, "paused_by": ["api"]}]),
        ("POST", "/control/resume"): (429, {"error": "debounced"}),
        ("PATCH", "/control/oc/0"): (200, {"id": 0, "source": "api", "profile": {"power": 230}, "applied": ["power=230 W"], "skipped": []}),
        ("PATCH", "/control/oc/1"): (409, {"id": 1, "source": "cli", "profile": {"core": 100, "mem": 800}}),
        ("PUT", "/control/oc/1"): lambda b: (200, {"id": 1, "source": "api", "profile": b, "applied": [], "skipped": ["fan: not supported"]}),
    })
    pm._req = http
    st = pm.stats()
    assert st.state == "running" and st.total_hashrate == 201e12 and pm.is_running()
    pm.pause_gpu(1)
    assert http.calls[-1] == ("POST", "/control/pause", {"gpus": [1]})
    with pytest.raises(RuntimeError):
        pm.resume_gpu(1)
    assert pm.oc_set(0, power=230)["applied"] == ["power=230 W"]
    r = pm.oc_set(1, fan={"duty": 70})                                  # 409 -> merged PUT keeps the cli profile
    assert http.calls[-1][0] == "PUT" and http.calls[-1][2] == {"core": 100, "mem": 800, "fan": {"duty": 70}}
    assert r["skipped"] == ["fan: not supported"]
    http.routes[("POST", "/control/pause")] = (404, {"error": "off"})
    with pytest.raises(NotImplementedError):
        pm.pause_gpu(0)


class FakeMinerOC:
    supports_oc_api = True

    def __init__(self):
        self.calls = []
        self.skip = []

    def stats(self):
        from agent.miner.base import GpuMinerStats, MinerStats
        return MinerStats(state="running", gpus=[GpuMinerStats(index=5, bus_id="01:00.0"), GpuMinerStats(index=6, bus_id="02:00.0")])

    def oc_set(self, index, **knobs):
        self.calls.append((index, knobs))
        return {"applied": [], "skipped": list(self.skip)}

    def oc_get(self, index):
        return {"id": index, "source": "api"}


def test_router_falls_back_to_miner_when_nvml_refuses(cfg, db, stack):
    _, _, gpu, _, _ = stack
    miner = FakeMinerOC()
    router = ControlRouter(gpu, miner, "auto")
    router.set_power_limit_w("GPU-SIM-0000", 230)                      # NVML(sim) accepts -> no miner call
    assert gpu.sample("GPU-SIM-0000").pl_w == 230 and miner.calls == []
    gpu.set_fan_pct = lambda uuid, pct: (_ for _ in ()).throw(NotSupported("fan refused"))
    router.set_fan_pct("GPU-SIM-0000", 70)                              # falls through to the miner, mapped by bus id
    assert miner.calls == [(5, {"fan": {"duty": 70}})] and router.last_path["GPU-SIM-0000:fan"] == "miner"
    miner.skip = ["fan: driver refused"]
    with pytest.raises(NotSupported):
        router.set_fan_pct("GPU-SIM-0001", 70)
    strict = ControlRouter(gpu, miner, "nvml")
    with pytest.raises(NotSupported):
        strict.set_fan_pct("GPU-SIM-0000", 70)
    assert router.capabilities("GPU-SIM-0000")["miner_oc_api"] is True


def test_managed_miner_lifecycle_is_live_in_dry_run_and_autostarts(cfg, db):
    assert cfg.dry_run is True
    st = service.build(cfg)
    st["collector"].register_gpus()
    st["miner"].stop()
    assert st["miner"].stats().state == "stopped"
    res = service.boot_restore(st)
    assert res["miner_started"] is True and st["miner"].stats().state == "running"
    r = st["actions"].miner_pause_gpu("GPU-SIM-0001", "rule", "test")  # GPU 1 has control_enabled=false: still live
    assert r.ok and not r.dry_run and st["miner"].stats().by_index()[1].paused
    r = st["actions"].set_power_limit("GPU-SIM-0000", 230, "rule", "test")  # GPU settings still obey dry_run
    assert r.ok and r.dry_run
    cfg.data["miner"]["managed"] = False
    r = st["actions"].miner_stop("user", "test")                        # stopping is always live
    assert r.ok and not r.dry_run and st["miner"].stats().state == "stopped"
    r = st["actions"].miner_start("user", "test")                       # starting is not, when unmanaged
    assert not r.ok and "managed=false" in r.error and st["miner"].stats().state == "stopped"
    # an unmanaged, stopped miner never triggers a watchdog restart
    for _ in range(50):
        st["watchdog"].tick()
    assert st["miner"].stats().state == "stopped" and db.count_actions_since("miner_restart", 0) == 0


def test_mem_lock_knob_and_3080_strategies(cfg, db):
    """Memory lock is a first-class setting; the RTX 3080 override carries the measured strategies."""
    from agent.optimizer import Optimizer
    cfg.data["safety"]["dry_run"] = False
    st = service.build(cfg)
    st["collector"].register_gpus()
    a = st["actions"]
    r = a.set_mem_lock("GPU-SIM-0000", 5001, "user", "t")
    assert r.ok and a.settings("GPU-SIM-0000")["mem_lock_mhz"] == 5001
    s = st["gpu"].sample("GPU-SIM-0000")
    assert s.mem_mhz == 5001 and s.power_w < 230                        # ~46 W saved in the sim model
    assert not a.set_mem_lock("GPU-SIM-0000", 4000, "user", "t").ok      # below clamp
    assert a.reset_mem_lock("GPU-SIM-0000", "user", "t").ok and a.settings("GPU-SIM-0000")["mem_lock_mhz"] is None
    # profile restore carries the lock
    a.apply_settings("GPU-SIM-0000", {"mem_lock_mhz": 5001}, "boot", "restore")
    assert a.settings("GPU-SIM-0000")["mem_lock_mhz"] == 5001
    # the real-card override produces core-lock + mem-lock steps, generic sim GPU keeps the defaults
    opt = Optimizer(cfg, db, a, st["trials"], st["collector"])
    steps = opt.strategy_steps("GPU-SIM-0000")
    assert steps["A"][0]["mem_lock_mhz"] is None and steps["B"][0]["core_offset_mhz"] == 0
    c3080 = cfg.clamps_for("NVIDIA GeForce RTX 3080")["optimize_strategies"]
    assert c3080["A"]["mem_lock_mhz"] == 5001 and c3080["B"]["core_offset_mhz"] == [75, 100, 125]
    assert cfg.get("optimize.default_goal") == "max-hashrate"
