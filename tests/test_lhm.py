from agent.platform.windows import LhmReader

TREE = {"Text": "Sensor", "ImageURL": "", "Children": [{"Text": "DESKTOP", "ImageURL": "images_icon/computer.png", "Children": [
    {"Text": "AMD Ryzen 3 3100", "ImageURL": "images_icon/cpu.png", "Children": [
        {"Text": "Temperatures", "ImageURL": "images_icon/temperature.png", "Children": [
            {"Text": "Core (Tctl/Tdie)", "Value": "52.4 °C", "Children": []}, {"Text": "CCD1 (Tdie)", "Value": "50.1 °C", "Children": []}]},
        {"Text": "Powers", "ImageURL": "images_icon/power.png", "Children": [{"Text": "Package", "Value": "41.3 W", "Children": []}]}]},
    {"Text": "NVIDIA GeForce RTX 3080", "ImageURL": "images_icon/nvidia.png", "Children": [
        {"Text": "Temperatures", "Children": [{"Text": "GPU Core", "Value": "67.0 °C", "Children": []},
                                             {"Text": "GPU Hot Spot", "Value": "86.1 °C", "Children": []},
                                             {"Text": "GPU Memory Junction", "Value": "72.0 °C", "Children": []}]},
        {"Text": "Fans", "Children": [{"Text": "GPU Fan 1", "Value": "2,180 RPM", "Children": []}]}]}]}]}


def test_lhm_matches_cpu_by_icon_and_gpu_memory_junction():
    r = LhmReader("http://x")
    r._tree, r._at, r.available = TREE, 1e12, True
    assert r.find("cpu", "Core (Tctl/Tdie)") == 52.4 and r.find("cpu", "Package") == 41.3
    assert r.gpu_memory_junction("RTX 3080", 0) == 72.0 and r.gpu_memory_junction("RTX 3080", 1) is None
    assert r.gpu_hotspot(0) == 86.1 and r.gpu_hotspot(1) is None
    assert r.find("nvidia", "GPU Fan 1") == 2180.0 and r.gpu_fan_rpm(0) == 2180.0 and r.gpu_fan_rpm(1) is None
    assert LhmReader._value({"Value": "52,4 °C"}) == 52.4 and LhmReader._value({"Value": "1,234.5 MHz"}) == 1234.5


def test_hotspot_rule_and_series(cfg, db, monkeypatch):
    from agent import service
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    st = service.build(cfg)
    st["collector"].register_gpus()
    gid = db.gpu_id("GPU-SIM-0000")
    import time
    now = time.time()
    for i in range(8):
        db.insert_gpu_sample(gid, now - (7 - i) * 300, "5m", power_w=390, core_c=71, memj_c=78, hashrate=133e12, accepted=i, rejected=0,
                             extra={"hotspot_c": 86 + i})
    from api import queries
    s = queries.series(db, gid, 2)
    assert s["hotspot_c"][-1] == 93 and len(s["hotspot_c"]) == 8
    from agent.tuner import Tuner
    e = Tuner(cfg, db, st["actions"]).evaluate("GPU-SIM-0000", "PRL")
    assert any("hot spot" in x for x in e["triggers"])
    # watchdog: sustained hot spot >= 100 escalates the overtemp ladder
    class C:
        t = now
    st2 = service.build(cfg, clock=lambda: C.t)
    st2["collector"].register_gpus()
    orig = st2["collector"].sample_gpus
    def hot_samples(*a, **k):
        rows = orig(*a, **k)
        for info, smp in rows:
            if smp is not None:
                smp.extra["hotspot_c"] = 101.0
        return rows
    st2["collector"].sample_gpus = hot_samples
    for _ in range(4):
        st2["watchdog"].tick()
        C.t += 15
    inc = db.one("SELECT summary FROM incidents WHERE rule='overtemp' ORDER BY id DESC LIMIT 1")
    assert inc and "hotspot 101" in inc["summary"]
