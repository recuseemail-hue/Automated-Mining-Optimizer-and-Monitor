import time

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def web(cfg, db, stack, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-test")
    monkeypatch.setenv("RIG_HUB_TOKEN", "hub-token")
    _, _, gpu, miner, col = stack
    col.collect_once()
    app = create_app(cfg, db)
    return TestClient(app, follow_redirects=False)


def login(c):
    r = c.post("/login", data={"password": "pw-test", "next": "/"})
    assert r.status_code == 303 and "rs=" in r.headers.get("set-cookie", "")


def test_auth_required_everywhere(web):
    assert web.get("/health").status_code == 200
    assert web.get("/").status_code == 303 and web.get("/").headers["location"].startswith("/login")
    assert web.get("/api/overview").status_code == 401
    assert web.post("/login", data={"password": "nope"}).status_code == 200      # form re-rendered, no cookie
    for _ in range(5):
        web.post("/login", data={"password": "nope"})
    r = web.post("/login", data={"password": "pw-test"})                         # locked out after 5 failures
    assert "rs=" not in r.headers.get("set-cookie", "")


def test_pages_and_overview(web):
    login(web)
    for path in ("/", "/gpus", "/log", "/claude", "/incidents", "/gpu/1"):
        r = web.get(path)
        assert r.status_code == 200 and "<main" in r.text, path
    o = web.get("/api/overview").json()
    assert o["gpus_total"] == 2 and o["hosts_online"] == 1 and o["total_hashrate"] > 1e14
    assert o["balance"]["coin"] == "PRL" and o["alerts"] == []
    gs = web.get("/api/gpus").json()
    assert gs[0]["sample"]["eff"] > 0 and gs[0]["profile"] is None
    s = web.get("/api/gpus/1/series?hours=24").json()
    assert len(s["ts"]) == 1 and s["eff"][0] > 0
    assert web.get("/api/gpus/1/series?hours=720").status_code == 200           # bucketed path
    rows = web.get("/api/log?limit=10").json()
    assert len(rows) == 2 and rows[0]["host"] == "testrig"
    csv = web.get("/api/log.csv").text.splitlines()
    assert csv[0].startswith("ts,host,idx,uuid,model") and len(csv) == 3
    assert web.get("/api/claude-runs").json() == [] and web.get("/api/incidents").json() == []
    assert web.get("/static/app.js").status_code == 200


def test_ingest_from_worker(web, db):
    hdr = {"Authorization": "Bearer hub-token"}
    assert web.post("/api/ingest", json={"host": "rig2", "items": []}).status_code == 401
    ts = time.time()
    body = {"host": "rig2", "items": [
        {"kind": "host_info", "payload": {"name": "rig2", "os": "Windows 11", "role": "worker", "meta": {}}},
        {"kind": "gpu_info", "payload": {"uuid": "GPU-W2-0", "idx": 0, "model": "RTX 4070", "bus_id": "x", "stock_pl_w": 200,
                                         "min_pl_w": 100, "max_pl_w": 220, "vram_mb": 12288, "control_enabled": False}},
        {"kind": "gpu_sample", "payload": {"uuid": "GPU-W2-0", "ts": ts, "res": "5m", "power_w": 150, "core_c": 60, "hashrate": 5e13,
                                           "accepted": 10, "rejected": 0, "throttle": [], "extra": {"sim": True}}},
        {"kind": "host_sample", "payload": {"host": "rig2", "ts": ts, "res": "5m", "cpu_pct": 5, "ram_used_mb": 3000, "ram_total_mb": 16000,
                                            "gpu_w_total": 150, "miner_state": "running", "coin": "PRL", "miner_hashrate": 5e13, "extra": {}}},
        {"kind": "action", "payload": {"ts": ts, "gpu_uuid": "GPU-W2-0", "action": "set_power_limit", "params": {"watts": 150}, "before": 200,
                                       "after": 150, "reason": "t", "actor": "rule", "dry_run": False, "ok": True}},
        {"kind": "incident", "payload": {"local_id": 7, "opened": ts, "gpu_uuid": "GPU-W2-0", "rule": "overtemp", "severity": "critical",
                                         "ladder_step": "power_floor", "status": "open", "summary": "hot"}},
        {"kind": "state", "payload": {"key": "watchdog.state", "value": {"ladder": {"GPU-W2-0": "power_floor"}}}},
        {"kind": "bogus", "payload": {}},
    ]}
    r = web.post("/api/ingest", json=body, headers=hdr)
    assert r.status_code == 200 and r.json()["accepted"] == 7
    o = web.get("/api/overview", headers=hdr).json()
    assert o["hosts_total"] == 2 and o["gpus_total"] == 3
    assert {a["rule"] for a in o["alerts"]} == {"overtemp", "power_floor"}
    w2 = next(g for g in o["gpus"] if g["host"] == "rig2")
    assert w2["ladder"] == "power_floor" and w2["sample"]["hashrate"] == 5e13
    # re-sent incident with closed status updates the same hub row
    body["items"] = [{"kind": "incident", "payload": {"local_id": 7, "gpu_uuid": "GPU-W2-0", "rule": "overtemp", "status": "recovered",
                                                      "closed": ts + 60, "ladder_step": "none", "summary": "cooled"}}]
    web.post("/api/ingest", json=body, headers=hdr)
    inc = web.get("/api/incidents", headers=hdr).json()
    assert len(inc) == 1 and inc[0]["status"] == "recovered" and inc[0]["host"] == "rig2"
