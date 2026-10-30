"""Optimizer: termination, time boxes, revert-on-unstable, winner application, crash restore."""
import pytest

from agent import service
from agent.optimizer import Optimizer, format_report


class FakeTime:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.sleeps = 0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1


@pytest.fixture
def opt_env(cfg, db):
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    ft = FakeTime()
    stack = service.build(cfg, clock=ft.clock)
    stack["collector"].register_gpus()
    opt = Optimizer(cfg, db, stack["actions"], stack["trials"], stack["collector"], clock=ft.clock, sleep=ft.sleep)
    return cfg, db, stack, opt, ft


def test_terminates_within_window_and_applies_winner(opt_env):
    cfg, db, stack, opt, ft = opt_env
    gpu = stack["gpu"]
    reps = opt.run(["GPU-SIM-0000"], "max-efficiency")
    r = reps[0]
    assert r.outcome == "applied" and r.winner is not None
    assert r.ended - r.started <= opt.per_gpu_s + 16                      # hard window respected
    assert len({s.strategy for s in r.steps}) == 3                        # every strategy got a turn
    assert all(s.verdict in ("better", "same", "worse", "unstable") for s in r.steps)
    # efficiency winner lowers the power limit; it is applied and saved as the active profile
    assert gpu.sample("GPU-SIM-0000").pl_w == r.winner.setting["pl_w"] < 240
    prof = db.profile(db.gpu_id("GPU-SIM-0000"), "active")
    assert prof["settings"]["pl_w"] == r.winner.setting["pl_w"] and prof["created_by"] == "optimizer"
    assert db.profile(db.gpu_id("GPU-SIM-0000"), "known_good")["settings"]["pl_w"] == 240.0
    assert db.open_trials() == []                                          # nothing left running
    # audit: every action row stayed inside the per-change step limit
    rows = db.query("SELECT before, after FROM actions WHERE action='set_power_limit' AND ok=1 AND actor='optimizer' "
                    "AND reason LIKE 'trial start%'")
    assert rows and all(abs(float(x["after"]) - float(x["before"])) <= 17.01 for x in rows)
    txt = format_report(reps)
    assert "baseline" in txt and "applied:" in txt


def test_unstable_step_reverts_and_ends_strategy(opt_env):
    cfg, db, stack, opt, ft = opt_env
    cfg.data["optimize"]["strategies"] = {"B": {"pl_pct": 70, "core_offset_mhz": [0, 0, 0, 0], "mem_offset_mhz": [500, 1100, 1200, 1200]}}
    cfg.data["optimize"]["per_gpu_s"] = 6000
    r = opt.run(["GPU-SIM-0000"], "max-efficiency")[0]
    verdicts = [(s.step, s.verdict) for s in r.steps]
    assert verdicts[0] == (1, "better") and verdicts[1][1] == "unstable" and len(verdicts) == 2
    assert "reject rate" in " ".join(r.steps[1].reasons) or "hashrate" in " ".join(r.steps[1].reasons)
    assert r.outcome == "applied" and r.winner.step == 1
    assert stack["gpu"].sample("GPU-SIM-0000").mem_offset_mhz == 500
    assert db.one("SELECT verdict FROM trials WHERE id=?", (r.steps[1].trial_id,))["verdict"] == "reverted"


def test_nothing_better_restores_original(opt_env):
    cfg, db, stack, opt, ft = opt_env
    # max-hashrate with only power-reducing strategies: every step is worse -> original restored
    cfg.data["optimize"]["strategies"] = {"C": {"pl_pct": [60, 55], "mem_offset_mhz": 0}}
    r = opt.run(["GPU-SIM-0000"], "max-hashrate")[0]
    assert r.outcome == "restored" and r.winner is None
    assert r.steps[0].verdict in ("worse", "unstable") and len(r.steps) == 1   # a bad step ends the strategy
    assert stack["gpu"].sample("GPU-SIM-0000").pl_w == 240.0


def test_rig_cap_skips_second_gpu(opt_env):
    cfg, db, stack, opt, ft = opt_env
    cfg.data["gpus"].append({"uuid": "GPU-SIM-0001", "control_enabled": True})
    cfg.data["optimize"]["rig_cap_s"] = 1500
    t0 = ft.t
    reps = opt.run(["GPU-SIM-0000", "GPU-SIM-0001"], "max-efficiency")
    assert len(reps) == 1 or reps[1].outcome == "aborted"
    assert ft.t - t0 <= 1500 + 16
    assert db.open_trials() == [] and stack["gpu"].sample("GPU-SIM-0001").pl_w == 240.0


def test_crash_mid_trial_restores(opt_env):
    cfg, db, stack, opt, ft = opt_env
    gpu = stack["gpu"]
    n = {"calls": 0}

    def dying_sleep(s):
        n["calls"] += 1
        ft.sleep(s)
        if n["calls"] == 60:                                   # ~15 min in: inside strategy A step 1
            raise KeyboardInterrupt
    opt.sleep = dying_sleep
    r = opt.run(["GPU-SIM-0000"], "max-efficiency")[0]
    assert r.outcome == "aborted" and "KeyboardInterrupt" in r.note
    assert gpu.sample("GPU-SIM-0000").pl_w == 240.0 and gpu.sample("GPU-SIM-0000").mem_offset_mhz == 0
    assert db.open_trials() == []


def test_hard_kill_is_reverted_by_watchdog_then_boot(opt_env):
    """Process dies without cleanup: trial row stays open; watchdog reverts at deadline; boot restore too."""
    cfg, db, stack, opt, ft = opt_env
    tr, gpu = stack["trials"], stack["gpu"]
    for _ in range(8):                                         # 2 min of 15 s rows for a baseline
        stack["watchdog"].tick()
        ft.t += 15
    t = tr.start("GPU-SIM-0000", {"pl_w": 221}, hold_s=120, kind="optimize", actor="optimizer", reason="x", stepped=True)
    assert "id" in t and gpu.sample("GPU-SIM-0000").pl_w == 221.0
    for _ in range(10):                                        # 150 s of watchdog ticks
        stack["watchdog"].tick()
        ft.t += 15
    row = db.one("SELECT verdict, reason FROM trials WHERE id=?", (t["id"],))
    assert row["verdict"] == "reverted" and "missed deadline" in row["reason"]
    assert gpu.sample("GPU-SIM-0000").pl_w == 240.0
    # and if the machine rebooted instead, boot_restore handles it
    t2 = tr.start("GPU-SIM-0000", {"pl_w": 221}, hold_s=600, kind="optimize", actor="optimizer", reason="x", stepped=True)
    fresh = service.build(cfg, clock=ft.clock)
    assert service.boot_restore(fresh)["aborted_trials"] == 1
    assert gpu.sample("GPU-SIM-0000").pl_w == 240.0


def test_refuses_without_control_or_under_protection(opt_env):
    cfg, db, stack, opt, ft = opt_env
    assert opt.run(["GPU-SIM-0001"], "max-efficiency")[0].outcome == "refused"
    db.kv_set("watchdog.state", {"ladder": {"GPU-SIM-0000": "power_floor"}})
    r = opt.run(["GPU-SIM-0000"], "max-efficiency")[0]
    assert r.outcome == "refused" and "protection" in r.note
