import json
import time
from pathlib import Path

from agent.db import SCHEMA_VERSION, Db
from agent.gpu.base import NotSupported
from agent.miner.peakminer import parse_summary
from agent.pool.kryptex import dig

FIX = Path(__file__).parent / "fixtures"


def test_config_merge_and_clamps(cfg):
    assert cfg.host_name == "testrig" and cfg.is_hub
    assert cfg.dry_run is True                      # default stays observe-only
    assert cfg.gpu_control_enabled("GPU-SIM-0000") and not cfg.gpu_control_enabled("GPU-SIM-0001")
    c = cfg.clamps_for("NVIDIA GeForce RTX 3080")
    assert c["power_limit_pct"] == [55, 100] and c["mem_step_mhz"] == 100
    assert cfg.clamps_for("Unknown GPU")["fan_pct"] == [40, 100]


def test_schema_idempotent(cfg):
    d1 = Db(cfg.db_path)
    d1.close()
    d2 = Db(cfg.db_path)                            # second open must not fail or duplicate
    assert d2.one("SELECT version FROM schema_version")["version"] == SCHEMA_VERSION
    assert d2.one("SELECT COUNT(*) c FROM schema_version")["c"] == 1
    d2.close()


def test_collect_once_writes_rows(stack):
    cfg, db, gpu, miner, col = stack
    summ = col.collect_once()
    assert summ["gpus"] == 2 and summ["miner"] == "running"
    rows = db.query("SELECT * FROM gpu_samples WHERE res='5m'")
    assert len(rows) == 2
    r = rows[0]
    assert r["power_w"] > 100 and r["hashrate"] > 1e13 and r["core_c"] > 30 and r["memj_c"] > r["core_c"]
    assert r["accepted"] is not None and r["pl_w"] == 240.0
    h = db.one("SELECT * FROM host_samples")
    assert h["gpu_w_total"] > 200 and h["miner_state"] == "running" and h["ram_total_mb"] > 0
    assert json.loads(h["extra"])["gpus_present"] == 2
    # registry rows carry stock limits and control flag from config
    g = db.one("SELECT * FROM gpus WHERE uuid='GPU-SIM-0000'")
    assert g["stock_pl_w"] == 340.0 and g["control_enabled"] == 1
    assert db.one("SELECT COUNT(*) c FROM outbox")["c"] == 0   # hub role: nothing queued


def test_worker_role_queues_outbox(stack):
    cfg, db, gpu, miner, col = stack
    cfg.data["host"]["role"] = "worker"
    col.collect_once()
    kinds = sorted(r["kind"] for r in db.outbox_peek(20))
    assert kinds == ["gpu_info", "gpu_info", "gpu_sample", "gpu_sample", "host_info", "host_sample"]
    col.collect_once()                               # registry rows are sent once, samples every time
    assert sorted(r["kind"] for r in db.outbox_peek(20)).count("gpu_info") == 2
    ids = [r["id"] for r in db.outbox_peek(20)]
    db.outbox_fail(ids[:1], "boom")
    assert db.outbox_peek(1)[0]["attempts"] == 1
    db.outbox_ack(ids)
    assert db.outbox_peek(10) == []


def test_sim_faults_and_missing_gpu(stack):
    cfg, db, gpu, miner, col = stack
    gpu.inject("GPU-SIM-0001", core_c=92, fan_rpm=0)
    s = gpu.sample("GPU-SIM-0001")
    assert s.core_c == 92 and s.fan_rpm == 0
    gpu.inject("GPU-SIM-0001", missing=True)
    assert [i.uuid for i in gpu.enumerate()] == ["GPU-SIM-0000"]
    try:
        gpu.sample("GPU-SIM-0001")
        assert False, "missing GPU must raise"
    except NotSupported:
        pass
    summ = col.collect_once()
    assert summ["gpus"] == 1
    gpu.clear_faults("GPU-SIM-0001")
    assert len(gpu.enumerate()) == 2


def test_sim_model_mem_offset_errors_not_crash(stack):
    """GDDR6X behaviour: past the edge, rejected shares rise and hashrate falls instead of a crash."""
    cfg, db, gpu, miner, col = stack
    base = gpu.model(gpu._gpu("GPU-SIM-0000"))["hashrate"]
    gpu.set_mem_offset_mhz("GPU-SIM-0000", 800)
    good = gpu.model(gpu._gpu("GPU-SIM-0000"))
    gpu.set_mem_offset_mhz("GPU-SIM-0000", 1300)
    bad = gpu.model(gpu._gpu("GPU-SIM-0000"))
    assert good["hashrate"] > base and good["rej_rate"] == 0
    assert bad["rej_rate"] > 0 and bad["hashrate"] < good["hashrate"]


def test_prune(stack):
    cfg, db, gpu, miner, col = stack
    gid = db.gpu_id("GPU-SIM-0000") or db.upsert_gpu(col.host_id, "GPU-SIM-0000", 0, "x", None, 1, 1, 1, 1, False)
    old = time.time() - 72 * 3600
    db.insert_gpu_sample(gid, old, "15s", power_w=1)
    db.insert_gpu_sample(gid, old, "5m", power_w=1)
    n = db.prune(retain_15s_hours=48, retain_5m_days=400)
    assert n["gpu_15s"] == 1 and n["gpu_5m"] == 0


def test_peakminer_parser_fixture():
    raw = json.loads((FIX / "peakminer_summary.json").read_text())
    st = parse_summary(raw, "H/s", "PRL")
    assert st.state == "running" and st.coin == "PRL" and st.algo == "pearl" and st.version == "2.20.5"
    assert len(st.gpus) == 2
    g0 = st.gpus[0]
    assert g0.hashrate == 101.2e12 and g0.accepted == 1532 and g0.rejected == 3 and g0.bus_id == "01:00.0"
    assert g0.temp_c == 68 and g0.power_w == 238 and g0.memj_c is None and not g0.paused
    assert st.total_hashrate == 201e12 and st.accepted == 3022 and st.rejected == 5
    assert st.pool == "prl.kryptex.network:7048" and st.worker == "rig1" and st.extra["dev_fee_percent"] == 2.0
    assert st.by_bus()["01:00.0"] is g0


def test_peakminer_parser_paused_and_shapes():
    raw = {"gpus": [{"id": 0, "hashrate": 0, "status": "paused", "accepted_shares": 1, "invalid_shares": 0},
                    {"id": 1, "hashrate": 5e12, "status": None, "accepted_shares": 2, "invalid_shares": 0}], "uptime": 120}
    st = parse_summary(raw, "H/s")
    assert st.state == "running" and st.gpus[0].paused and not st.gpus[1].paused and st.total_hashrate == 5e12
    raw["gpus"][1]["status"] = "paused"
    assert parse_summary(raw, "H/s").state == "paused"
    assert parse_summary([], "H/s").state == "error"
    assert parse_summary({"gpus": [{"id": 0, "hashrate": 5}]}, "kH/s").gpus[0].hashrate == 5000


def test_dig():
    assert dig({"a": {"b": [1, {"c": 2}]}}, "a.b.1.c") == 2
    assert dig({"a": 1}, "a.b") is None and dig({}, None) is None
