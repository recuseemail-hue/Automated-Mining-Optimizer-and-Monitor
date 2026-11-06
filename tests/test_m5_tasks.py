"""Hourly tuner, coin selector, Claude runner wrapper (fake CLI), scheduler plan."""
import json
import time

import pytest

from agent import service
from agent.claude_runner import ClaudeRunner
from agent.context import build_context
from agent.scheduler import plan, systemd_units, windows_commands
from agent.selector import Selector
from agent.tuner import Tuner


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(cfg, db):
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    clock = Clock()
    st = service.build(cfg, clock=clock)
    st["collector"].register_gpus()
    return cfg, db, st, clock


def seed_rows(db, gid, clock, hours=2, core=68.0, hashrate=100e12, power=236.0, rej=0, mem_offset=0):
    now = clock()
    n = int(hours * 12)
    for i in range(n):
        ts = now - (n - 1 - i) * 300
        db.insert_gpu_sample(gid, ts, "5m", power_w=power, core_c=core, memj_c=core + 18, fan_pct=70, core_mhz=1400, mem_mhz=9501,
                             pl_w=240, util_pct=100, hashrate=hashrate, accepted=10 * (i + 1), rejected=rej * (i + 1),
                             mem_offset_mhz=mem_offset, core_offset_mhz=0, throttle=[], extra={})


def test_tuner_no_candidates_when_within_targets(env):
    cfg, db, st, clock = env
    gid = db.gpu_id("GPU-SIM-0000")
    seed_rows(db, gid, clock)
    pre = Tuner(cfg, db, st["actions"], clock=clock).precheck("PRL")
    assert pre["candidates"] == [] and all(not g["candidate"] for g in pre["gpus"])


def test_tuner_candidate_worst_offender_and_proposal(env):
    cfg, db, st, clock = env
    cfg.data["gpus"].append({"uuid": "GPU-SIM-0001", "control_enabled": True})
    g0, g1 = db.gpu_id("GPU-SIM-0000"), db.gpu_id("GPU-SIM-0001")
    seed_rows(db, g0, clock, core=78)                       # 3 C over target
    seed_rows(db, g1, clock, core=84)                       # 9 C over target -> worst
    t = Tuner(cfg, db, st["actions"], clock=clock)
    pre = t.precheck("PRL")
    assert pre["candidates"] == ["GPU-SIM-0001"] and pre["skipped_candidates"] == ["GPU-SIM-0000"]
    c = next(g for g in pre["gpus"] if g["uuid"] == "GPU-SIM-0001")
    assert c["proposal"]["key"] == "pl_w" and c["proposal"]["value"] == 223.0    # one 5 % step down
    # instability trigger backs off memory offset; below-baseline trigger uses saved profile score
    seed_rows(db, g0, clock, core=68, rej=1, mem_offset=300)
    e = t.evaluate("GPU-SIM-0000", "PRL")
    assert any("reject" in x for x in e["triggers"])
    st["gpu"].set_mem_offset_mhz("GPU-SIM-0000", 300)
    assert t.propose("GPU-SIM-0000", t.last_hour(g0), cfg.targets_for("GPU-SIM-0000"), None, e["triggers"])["key"] == "mem_offset_mhz"


def test_tuner_exclusions_converged_cap_and_ladder(env):
    cfg, db, st, clock = env
    gid = db.gpu_id("GPU-SIM-0000")
    seed_rows(db, gid, clock, core=80)
    t = Tuner(cfg, db, st["actions"], clock=clock)
    db.save_profile(gid, {"pl_w": 240}, "converged", converged_until=clock() + 86400)
    e = t.evaluate("GPU-SIM-0000", "PRL")
    assert e["candidate"] is True                                          # a trigger overrides convergence
    seed_rows(db, gid, clock, core=68)
    e = t.evaluate("GPU-SIM-0000", "PRL")
    assert not e["candidate"] and e["triggers"] == []
    seed_rows(db, gid, clock, core=80)
    for i in range(6):
        db.insert_trial(gid, "hourly", {"pl_w": 230}, {}, clock() - 100 * (i + 1), clock() - 50)
    assert "trial cap 6/6 today" in t.evaluate("GPU-SIM-0000", "PRL")["excluded"]
    db.kv_set("watchdog.state", {"ladder": {"GPU-SIM-0000": "paused"}})
    assert any("watchdog" in x for x in t.evaluate("GPU-SIM-0000", "PRL")["excluded"])


def test_convergence_after_three_flat_trials(env):
    cfg, db, st, clock = env
    tr, gid = st["trials"], db.gpu_id("GPU-SIM-0000")
    for _ in range(10):
        st["watchdog"].tick()
        clock.t += 15
    for k in range(3):
        t = tr.start("GPU-SIM-0000", {"fan_pct": 70 + 10 * k}, hold_s=120, reason="flat")   # sim: no efficiency effect
        assert "id" in t
        for _ in range(12):
            st["watchdog"].tick()
            clock.t += 15
        assert db.one("SELECT verdict FROM trials WHERE id=?", (t["id"],))["verdict"] in ("reverted", "kept")
    conv = db.profile(gid, "converged")
    assert conv is not None and conv["converged_until"] > clock() + 6 * 86400
    assert not Tuner(cfg, db, st["actions"], clock=clock).evaluate("GPU-SIM-0000", "PRL")["candidate"]


def test_selector_hysteresis_and_fork(env):
    cfg, db, st, clock = env
    cfg.data["coins"]["XTM"]["miner"] = "peakminer"           # pretend a Tari backend exists for this test
    sel = Selector(cfg, db, clock=clock)
    # seed data is stale -> never switches
    d = sel.decide(["RTX 3080"], "PRL")
    assert d["decision"] == "stay" and "no fresh" in d["reasoning"][0]
    # learned perf + fresh live data: XTM 20% better -> first check holds, second check switches
    sel.learn_perf("RTX 3080", "PRL", 105e12, 240)
    sel.learn_perf("RTX 3080", "XTM", 1.0, 192)
    sel.learn_perf("RTX 3080", "QTC", 488e6, 165)
    cfg.data["selector"]["source"]["provider"] = "http_json"
    db.kv_set("selector.cache", None)
    live = {"PRL": {"reward": 2.0 / 105e12, "unit": "H", "price": 1.0, "age_s": 60},
            "XTM": {"reward": 2.4, "unit": "H", "price": 1.0, "age_s": 60},
            "QTC": {"reward": 3.0 / 488e6, "unit": "H", "price": 1.0, "age_s": 60}}
    sel.fetch_raw = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    db.kv_set("selector.cache", {"ts": clock(), "live": live})
    d1 = sel.decide(["RTX 3080"], "PRL")
    assert d1["decision"] == "stay" and d1["streak"] == {"coin": "XTM", "n": 1}
    assert d1["meta"]["from_cache"] is True
    d2 = sel.decide(["RTX 3080"], "PRL")
    assert d2["decision"] == "switch:XTM" and d2["target"] == "XTM"
    # QTC is the most profitable but auto_switch=false: never chosen automatically
    assert all(e["coin"] != "QTC" or e["net_usd_day"] > 2.5 for e in d2["estimates"])
    # stale cache -> no switch
    db.kv_set("selector.cache", {"ts": clock() - 8 * 3600, "live": live})
    assert sel.decide(["RTX 3080"], "PRL")["decision"] == "stay"
    # fork activated: PRL unminable on a 3080 -> forced switch to the best fresh approved coin
    db.kv_set("selector.cache", {"ts": clock(), "live": live})
    sel.set_fork(activation_height=1234567, activated=True, note="test")
    d3 = sel.decide(["RTX 3080"], "PRL")
    assert d3["decision"] == "switch:XTM" and "no longer minable" in d3["reasoning"][0]
    assert sel.minable("PRL", "NVIDIA GeForce RTX 5090") and not sel.minable("PRL", "RTX 3080")
    assert db.one("SELECT COUNT(*) c FROM coin_checks")["c"] > 0


def test_switch_coin_action(env, monkeypatch):
    cfg, db, st, clock = env
    a = st["actions"]
    r = a.switch_coin("XTM", "claude", "t")
    assert not r.ok and "no miner backend" in r.error
    cfg.data["coins"]["XTM"]["miner"] = "peakminer"
    r = a.switch_coin("XTM", "claude", "t")
    assert not r.ok and "no wallet secret" in r.error
    monkeypatch.setenv("RIG_WALLET_XTM", "xtm-addr")
    r = a.switch_coin("XTM", "claude", "t")
    assert r.ok and r.before == "PRL" and r.after == "XTM"
    assert st["miner"].stats().coin == "XTM" and db.kv_get("miner.coin_override") == "XTM"
    cfg.data["coins"]["CFX"]["miner"] = "peakminer"
    assert "RateLimited" in a.switch_coin("CFX", "claude", "t").error            # 1 per day
    assert "owner approval" in a.switch_coin("QTC", "claude", "t", force=True).error
    fresh = service.build(cfg, clock=clock)
    assert fresh["miner"].active_coin()["coin"] == "XTM"                          # survives restart


def test_claude_runner_gates_and_logging(env, monkeypatch, tmp_path):
    cfg, db, st, clock = env
    calls = []

    def fake(args, prompt, env_, timeout):
        calls.append((args, prompt, env_))
        return {"rc": 0, "stdout": json.dumps({"result": "GPU 0: core 80C -> started trial pl_w 223\nothers: untouched",
                                               "total_cost_usd": 0.0, "usage": {"input_tokens": 100, "output_tokens": 20},
                                               "is_error": False}), "stderr": ""}
    cr = ClaudeRunner(cfg, db, clock=clock, runner=fake)
    ctx = build_context(cfg, st, "hourly")
    # no token -> skipped, logged
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("agent.claude_runner.shutil.which", lambda x: "/usr/bin/claude")
    r = cr.run_task("hourly", "manual", ctx)
    assert r["status"] == "skipped" and "OAUTH" in r["summary"]
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-secret-value")
    r = cr.run_task("hourly", "schedule", ctx, schedule="5 * * * *")
    assert r["status"] == "ok" and r["summary"].startswith("GPU 0")
    args, prompt, env_ = calls[0]
    assert args[:3] == ["claude", "-p", "--output-format"] and "--no-session-persistence" in args
    assert "Bash(rigctl trial *)" in args[args.index("--allowedTools") + 1] and "--max-budget-usd" not in args
    assert env_["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-secret-value" and env_["RIG_RUN_ID"] == r["run_id"]
    assert "Hourly check" in prompt and '"candidates"' in prompt
    row = db.one("SELECT * FROM claude_runs WHERE run_id=?", (r["run_id"],))
    assert row["status"] == "ok" and row["tokens_in"] == 100 and row["schedule"] == "5 * * * *"
    # daily cap: scheduled runs stop at cap, incidents continue to 2x
    cfg.data["claude"]["daily_cap"] = 1
    assert cr.run_task("hourly", "schedule", ctx)["status"] == "skipped"
    ictx = build_context(cfg, st, "incident", incident_id=None)
    assert cr.run_task("incident", "watchdog", ictx)["status"] == "ok"
    # RAM gate applies to scheduled runs only
    cfg.data["claude"]["daily_cap"] = 50
    monkeypatch.setattr("agent.claude_runner.psutil.virtual_memory", lambda: type("vm", (), {"available": 100 * 2**20})())
    assert cr.run_task("hourly", "schedule", ctx)["status"] == "skipped"
    assert cr.run_task("incident", "watchdog", ictx)["status"] == "ok"
    # secrets never appear in stored output
    assert "tok-secret-value" not in (db.one("SELECT output FROM claude_runs ORDER BY id DESC LIMIT 1")["output"] or "")


def test_scheduler_plan(cfg):
    p = plan(cfg)
    assert {x["task"]: x["kind"] for x in p} == {"hourly": "hourly", "algo_check": "daily"}
    cmds = windows_commands(cfg)
    assert any("/SC" in c and "HOURLY" in c for c in cmds) and all("/RU" in c for c in cmds)
    units = systemd_units(cfg)
    assert "rig-overseer-hourly.timer" in units and "OnCalendar=*-*-* *:05:00" in units["rig-overseer-hourly.timer"]
    cfg.data["selector"]["schedule"] = "weekly"
    assert next(x for x in plan(cfg) if x["task"] == "algo_check")["kind"] == "weekly"
