"""Watchdog, action API and trial manager against simulated telemetry with a fake clock."""
import json

import pytest

from agent import service


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def live(cfg, db):
    """Control enabled on GPU 0, dry_run off, emergency override on, notifier offline (no network)."""
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "ntfy"            # NTFY_TOPIC unset -> every send fails -> queued
    clock = Clock()
    stack = service.build(cfg, clock=clock)
    stack["clock"] = clock
    stack["collector"].register_gpus()
    return stack


def ticks(stack, n, dt=15):
    out = []
    for _ in range(n):
        out.append(stack["watchdog"].tick())
        stack["clock"].advance(dt)
    return out


def test_dry_run_logs_but_does_not_apply(cfg, db):
    stack = service.build(cfg, clock=Clock())
    stack["collector"].register_gpus()
    a = stack["actions"]
    r = a.set_power_limit("GPU-SIM-0000", 230, "claude", "test")
    assert r.ok and r.dry_run and r.after is None
    assert stack["gpu"].sample("GPU-SIM-0000").pl_w == 240.0
    row = db.one("SELECT * FROM actions ORDER BY id DESC LIMIT 1")
    assert row["dry_run"] == 1 and row["actor"] == "claude" and json.loads(row["params"]) == {"watts": 230}


def test_control_flag_and_clamps(live):
    a = live["actions"]
    # GPU 1 has control_enabled=false -> refused and logged
    r = a.set_power_limit("GPU-SIM-0001", 230, "claude", "test")
    assert not r.ok and "control_enabled" in r.error
    # clamp: 55% of 340 = 187 W floor
    r = a.set_power_limit("GPU-SIM-0000", 150, "claude", "test")
    assert not r.ok and "ClampError" in r.error
    # step: 5% of 340 = 17 W per action
    r = a.set_power_limit("GPU-SIM-0000", 200, "claude", "test")
    assert not r.ok and "exceeds max step" in r.error
    r = a.set_power_limit("GPU-SIM-0000", 230, "claude", "test")
    assert r.ok and r.before == 240.0 and r.after == 230.0
    # memory step 100 MHz; range up to +1200
    assert not a.set_mem_offset("GPU-SIM-0000", 200, "claude", "t").ok
    assert a.set_mem_offset("GPU-SIM-0000", 100, "claude", "t").ok
    assert not a.set_mem_offset("GPU-SIM-0000", 1300, "claude", "t", emergency=True).ok   # clamp survives emergency
    # unknown coin / non-approved coin refused
    assert "not in config catalog" in a.switch_coin("DOGE", "claude", "t").error
    r = a.miner_restart("claude", "t")
    assert r.ok
    for _ in range(2):
        a.miner_restart("claude", "t")
    assert "RateLimited" in a.miner_restart("claude", "t").error


def test_overheat_ladder_then_recovery_without_network(live):
    wd, gpu, miner, db, clock = live["watchdog"], live["gpu"], live["miner"], live["db"], live["clock"]
    ticks(live, 2)                                   # learn expected GPUs, baseline
    gpu.inject("GPU-SIM-0000", core_c=88)
    ticks(live, 3)                                   # 30 s sustained -> step 1
    assert gpu.sample("GPU-SIM-0000").pl_w == 187.0  # 55% of 340
    inc = db.one("SELECT * FROM incidents WHERE rule='overtemp'")
    assert inc and inc["ladder_step"] == "power_floor"
    assert db.one("SELECT COUNT(*) c FROM outbox WHERE kind='notify'")["c"] >= 1   # alert queued, nothing blocked
    ticks(live, 5)                                   # +60 s still hot -> step 2 pause
    assert miner.stats().by_index()[0].paused
    ticks(live, 5)                                   # -> step 3 stop miner
    assert miner.stats().state == "stopped"
    assert wd.gpus["GPU-SIM-0000"].ladder == 3
    # clear the fault; after recover_after_s the miner is restarted and the GPU resumed
    gpu.clear_faults("GPU-SIM-0000")
    ticks(live, 45)                                  # ~11 min
    assert miner.stats().state == "running" and not miner.stats().by_index()[0].paused
    assert wd.gpus["GPU-SIM-0000"].ladder == 0
    assert gpu.sample("GPU-SIM-0000").pl_w == 187.0  # power floor deliberately kept
    assert db.one("SELECT status FROM incidents WHERE rule='overtemp'")["status"] == "recovered"
    acts = [r["action"] for r in db.query("SELECT action FROM actions WHERE actor='rule' ORDER BY id")]
    assert acts[:3] == ["set_power_limit", "miner_pause_gpu", "miner_stop"]


def test_hard_limit_shuts_down(live, monkeypatch):
    calls = []
    monkeypatch.setattr(live["plat"], "shutdown", lambda reason, delay_s=30: calls.append(reason))
    live["gpu"].inject("GPU-SIM-0000", core_c=97)
    ticks(live, 4)
    assert calls and "hard_temp" in calls[0]
    assert live["miner"].stats().state == "stopped"
    assert live["notifier"].sent[0]["priority"] == "urgent"


def test_fan_failure_and_gpu_missing(live):
    gpu, db = live["gpu"], live["db"]
    ticks(live, 2)
    gpu.inject("GPU-SIM-0001", fan_rpm=0, fan_pct=0)
    ticks(live, 6)                                   # 60 s sustained
    inc = db.one("SELECT * FROM incidents WHERE rule='fan_failure'")
    assert inc is not None
    # GPU 1 has control disabled: power-limit step is refused, logged, ladder still advances
    assert db.one("SELECT ok FROM actions WHERE action='set_power_limit' AND actor='rule'")["ok"] == 0
    gpu.clear_faults("GPU-SIM-0001")
    gpu.inject("GPU-SIM-0000", missing=True)
    ticks(live, 4)
    assert db.one("SELECT * FROM incidents WHERE rule='gpu_missing'") is not None
    assert live["miner"].stats().state == "stopped"


def test_miner_down_restart_then_crash_loop_cap(live):
    miner, db = live["miner"], live["db"]
    ticks(live, 1)
    miner.stop()
    ticks(live, 41)                                  # > 600 s -> restart
    assert miner.stats().state == "running"
    assert db.count_actions_since("miner_restart", 0) == 1
    for _ in range(3):                               # dies again right after each restart -> fast retries, cap at 3/30 min
        miner.stop()
        ticks(live, 6)
    assert db.count_actions_since("miner_restart", 0) == 3
    assert db.one("SELECT * FROM incidents WHERE rule='miner_down'") is not None
    assert miner.stats().state == "stopped"


def test_overpower_trims_hungriest(live):
    live["cfg"] = live["collector"].cfg
    live["cfg"].data["limits"]["psu_budget_w"] = 400         # two sim GPUs draw ~470 W
    live["cfg"].data["gpus"].append({"uuid": "GPU-SIM-0001", "control_enabled": True})
    ticks(live, 8)
    pls = sorted(live["gpu"].sample(u).pl_w for u in ("GPU-SIM-0000", "GPU-SIM-0001"))
    assert pls[0] < 240.0 and live["db"].one("SELECT * FROM incidents WHERE rule='overpower'") is not None


def test_trial_revert_on_deadline_and_boot_restore(live):
    tr, a, gpu, db, clock = live["trials"], live["actions"], live["gpu"], live["db"], live["clock"]
    gpu.set_mem_offset_mhz("GPU-SIM-0000", 900)      # pre-existing state (as if from a saved profile)
    ticks(live, 8)                                   # 2 min of baseline rows
    # one step up, still inside the stable zone of the sim model -> kept and saved as active profile
    t1 = tr.start("GPU-SIM-0000", {"mem_offset_mhz": 1000}, hold_s=300, reason="unit")
    assert "id" in t1 and gpu.sample("GPU-SIM-0000").mem_offset_mhz == 1000
    assert "already open" in tr.start("GPU-SIM-0000", {"pl_w": 230}, 300)["error"]
    ticks(live, 10)                                  # 150 s: not yet due
    assert db.open_trials()
    ticks(live, 12)                                  # past deadline -> judged by the watchdog tick
    assert db.one("SELECT verdict FROM trials WHERE id=?", (t1["id"],))["verdict"] == "kept"
    assert db.profile(db.gpu_id("GPU-SIM-0000"), "active")["settings"]["mem_offset_mhz"] == 1000
    # next step crosses the GDDR6X error edge: rejects rise, hashrate drops -> reverted automatically
    t2 = tr.start("GPU-SIM-0000", {"mem_offset_mhz": 1100}, hold_s=300, reason="unit")
    ticks(live, 22)
    row = db.one("SELECT * FROM trials WHERE id=?", (t2["id"],))
    assert row["verdict"] == "reverted" and ("reject rate" in row["reason"] or "hashrate" in row["reason"])
    assert gpu.sample("GPU-SIM-0000").mem_offset_mhz == 1000
    # clamp and step limits apply to trials too
    assert "ClampError" in tr.start("GPU-SIM-0000", {"mem_offset_mhz": 1300}, 300)["error"]
    assert "exceeds max step" in tr.start("GPU-SIM-0000", {"pl_w": 200}, 300)["error"]
    # crash mid-trial: open trial row + changed hardware, then a fresh service boots
    t3 = tr.start("GPU-SIM-0000", {"pl_w": 230}, hold_s=900, reason="unit")
    assert gpu.sample("GPU-SIM-0000").pl_w == 230.0
    fresh = service.build(live["collector"].cfg, clock=clock)
    res = service.boot_restore(fresh)
    assert res["aborted_trials"] == 1
    assert gpu.sample("GPU-SIM-0000").pl_w == 240.0 and gpu.sample("GPU-SIM-0000").mem_offset_mhz == 1000
    assert db.one("SELECT verdict FROM trials WHERE id=?", (t3["id"],))["verdict"] == "aborted"
    assert res["restored"]["GPU-SIM-0000"] == "ok"
