"""Incident escalation to Claude, heartbeat, shutdown notifications."""
import pytest

from agent import service
from agent.heartbeat import Heartbeat
from agent.incidents import IncidentEscalator


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(cfg, db, monkeypatch):
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    monkeypatch.setenv("HEALTHCHECKS_URL", "https://hc-ping.example/uuid")
    clock = Clock()
    st = service.build(cfg, clock=clock)
    st["collector"].register_gpus()
    return cfg, db, st, clock


def test_watchdog_incident_launches_one_claude_run(env):
    cfg, db, st, clock = env
    launched = []
    st["escalator"].spawn = lambda iid: launched.append(iid)
    st["gpu"].inject("GPU-SIM-0000", core_c=88)
    for _ in range(12):                                   # overtemp -> ladder steps 1 and 2 on the same incident
        st["watchdog"].tick()
        clock.t += 15
    assert len(launched) == 1
    iid = launched[0]
    assert db.one("SELECT rule FROM incidents WHERE id=?", (iid,))["rule"] == "overtemp"
    assert db.kv_get(f"incident.{iid}.claude")["launched"] == pytest.approx(clock.t, abs=200)
    # a second, different incident within the gap is suppressed; after the gap it launches
    st["gpu"].inject("GPU-SIM-0001", fan_rpm=0, fan_pct=0)
    for _ in range(6):
        st["watchdog"].tick()
        clock.t += 15
    assert len(launched) == 1
    clock.t += 1000
    st["gpu"].inject("GPU-SIM-0001", fan_rpm=0, fan_pct=0, missing=True)
    for _ in range(4):
        st["watchdog"].tick()
        clock.t += 15
    assert len(launched) == 2


def test_escalator_disabled_and_failure_notifies(env):
    cfg, db, st, clock = env
    iid = db.open_incident("overtemp", "critical", host_id=st["collector"].host_id, gpu_id=1, summary="x")
    cfg.data["claude"]["incident_runs"] = False
    assert st["escalator"].on_incident(iid) is False
    cfg.data["claude"]["incident_runs"] = True

    def boom(_):
        raise OSError("no python")
    esc = IncidentEscalator(cfg, db, st["notifier"], clock=clock, spawn=boom)
    assert esc.on_incident(iid) is False
    assert st["notifier"].sent[-1]["title"].endswith("Claude launch failed")


def test_heartbeat_ping_and_fail_before_shutdown(env, monkeypatch):
    cfg, db, st, clock = env
    calls = []
    hb = Heartbeat(cfg, clock=clock, fetch=lambda url, body=None: calls.append((url, body)))
    assert hb.ping() and calls[-1] == ("https://hc-ping.example/uuid", None)
    st["actions"].heartbeat = hb
    monkeypatch.setattr(st["plat"], "shutdown", lambda reason, delay_s=30: calls.append(("SHUTDOWN", reason)))
    r = st["actions"].shutdown("rule", "hard_temp: core 97C", emergency=True)
    assert r.ok
    kinds = [c[0] for c in calls]
    assert kinds[-2] == "https://hc-ping.example/uuid/fail" and kinds[-1] == "SHUTDOWN"   # fail ping precedes the OS call
    assert st["notifier"].sent[-1]["priority"] == "urgent" and "shutdown in" in st["notifier"].sent[-1]["title"]
    # a failing endpoint never raises into the caller
    bad = Heartbeat(cfg, clock=clock, fetch=lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert bad.ping() is False and bad.failures == 1
    monkeypatch.delenv("HEALTHCHECKS_URL")
    assert Heartbeat(cfg, clock=clock).ping() is False


def test_incident_context_bundle(env):
    cfg, db, st, clock = env
    from agent.context import build_context
    for _ in range(8):
        st["watchdog"].tick()
        clock.t += 15
    iid = db.open_incident("fan_failure", "critical", host_id=st["collector"].host_id, gpu_id=db.gpu_id("GPU-SIM-0000"),
                           ladder_step="power_floor", summary="fan 0 RPM", opened=clock())
    st["actions"].set_power_limit("GPU-SIM-0000", 230, "rule", "fan_failure", emergency=True)
    ctx = build_context(cfg, st, "incident", incident_id=iid)
    assert ctx["incident"]["rule"] == "fan_failure"
    tel = ctx["telemetry_30min"]
    assert list(tel) == ["GPU-SIM-0000"] and tel["GPU-SIM-0000"]["summary"]["n"] == 8
    assert ctx["actions_30min"][-1]["action"] == "set_power_limit" and ctx["actions_30min"][-1]["actor"] == "rule"
    assert "logs" in ctx and "gpu_table" in ctx
