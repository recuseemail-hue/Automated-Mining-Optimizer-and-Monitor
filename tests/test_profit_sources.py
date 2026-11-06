"""hashrate.no provider (v1 coins + gpuEstimates): parsing, caching, call cap, selector integration."""
import pytest

from agent import service
from agent.profit_sources import HashrateNoSource, gpu_key, parse_unit_hs
from agent.selector import Selector


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


COINS = [
    {"coin": "PRL", "name": "Pearl", "algo": "pearlhash", "usdPrice": "0.0421", "coinEstimate": "0.5012", "coinEstimateUnit": "1 TH/s",
     "usdEstimate": "0.0211", "usdEstimate24h": "0.0205", "networkHashrate": "12.3 PH/s"},
    {"coin": "QUAN", "name": "Quantus", "algo": "quantus", "usdPrice": "1.2", "coinEstimate": "0.0029", "coinEstimateUnit": "1 MH/s",
     "usdEstimate": "0.00346", "usdEstimate24h": "0.0034", "networkHashrate": "1 TH/s"},
    {"coin": "XTM", "name": "Tari", "algo": "cuckaroo29", "usdPrice": "0.01", "coinEstimate": "100", "coinEstimateUnit": "1 H/s",
     "usdEstimate": "2.6", "networkHashrate": "1"},
]
GPUS = {"3080": {"device": {"name": "RTX 3080", "brand": "NVIDIA"}, "profit": {"coin": "PRL", "revenueUSD": 2.6, "profitUSD": 2.6},
                 "revenue": {"coin": "PRL", "revenueUSD": 2.6, "profitUSD": 2.6}, "profit24": {"coin": "PRL", "revenueUSD24": 2.5, "profitUSD24": 2.5},
                 "revenue24": {"coin": "PRL", "revenueUSD24": 2.5, "profitUSD24": 2.5}},
        "4070": {"device": {"name": "RTX 4070", "brand": "NVIDIA"}, "profit": {"coin": "QUAN", "revenueUSD": 1.1, "profitUSD": 1.1}}}


def fake_fetch(calls):
    def f(url):
        calls.append(url)
        if "/coins?" in url:
            return COINS
        if "/gpuEstimates?" in url:
            return GPUS
        return {"title": "Not Found", "detail": url}
    return f


def test_gpu_key_and_units():
    assert gpu_key("NVIDIA GeForce RTX 3080") == "3080" and gpu_key("RTX 3080 Ti") == "3080Ti" and gpu_key("Radeon RX 6800") is None
    assert parse_unit_hs("1 MH/s") == 1e6 and parse_unit_hs("kH/s") == 1e3 and parse_unit_hs("100 GH/s") == 1e11
    assert parse_unit_hs("1 TH/s") == 1e12 and parse_unit_hs("bogus") is None and parse_unit_hs(None) is None


@pytest.fixture
def env(cfg, db, monkeypatch):
    monkeypatch.setenv("HASHRATE_NO_KEY", "hn-key-123456")
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    clock = Clock()
    st = service.build(cfg, clock=clock)
    st["collector"].register_gpus()
    return cfg, db, st, clock


def test_coins_cache_cap_and_gpu_best(env):
    cfg, db, st, clock = env
    calls = []
    src = HashrateNoSource(cfg, db, clock=clock, fetch=fake_fetch(calls))
    coins, meta = src.coins()
    assert "apiKey=hn-key-123456" in calls[0] and calls[0].endswith("/coins?apiKey=hn-key-123456")
    assert coins["PRL"]["usd_per_hs"] == pytest.approx(0.0211 / 1e12) and coins["QUAN"]["usd_per_hs"] == pytest.approx(0.00346 / 1e6)
    assert meta["source"] == "hashrate_no"
    src.coins()
    assert len(calls) == 1                                               # cached within the TTL
    best, m2 = src.gpu_best("NVIDIA GeForce RTX 3080")
    assert best["key"] == "3080" and best["profit"]["coin"] == "PRL" and "powerCost=0.0" in calls[1]
    assert src.gpu_best("NVIDIA GeForce RTX 4070")[0]["key"] == "4070" and src.gpu_best("RX 6800")[0] is None
    # per-model estimates use OUR expected hashrate; coins without a perf entry are skipped
    est, _ = src.estimates("RTX 3080", lambda model, coin: {"hashrate": 125e12, "power_w": 377} if coin == "PRL" else None)
    assert est["PRL"]["revenue_usd_day"] == pytest.approx(0.0211 * 125) and est["PRL"]["hashrate"] == 125e12
    assert est["QTC"]["revenue_usd_day"] is None and est["QTC"]["usd_per_hs"] == pytest.approx(0.00346 / 1e6)  # QUAN symbol mapped
    # monthly cap: live calls refused, cache still served with the error attached
    clock.t += 7 * 3600
    cfg.data["selector"]["hashrate_no"]["monthly_call_cap"] = 2
    src = HashrateNoSource(cfg, db, clock=clock, fetch=fake_fetch(calls))
    coins, meta = src.coins()
    assert len(calls) == 2 and meta["source"] == "hashrate_no(cache)" and "cap" in meta["error"]
    # API error envelope is surfaced, not parsed as data
    cfg.data["selector"]["hashrate_no"]["monthly_call_cap"] = 90
    db.kv_set("hashrate_no.cache.coins", None)
    bad = HashrateNoSource(cfg, db, clock=clock, fetch=lambda url: {"title": "Unauthorized", "detail": "bad key"})
    coins, meta = bad.coins()
    assert coins == {} and "Unauthorized" in meta["error"]
    probe = src.probe("NVIDIA GeForce RTX 3080")
    assert probe["coins_count"] == 3 and probe["gpu_best"]["key"] == "3080" and "PRL" in probe["catalog_coins"]


def test_selector_uses_hashrate_no_with_learned_hashrate(env, monkeypatch):
    cfg, db, st, clock = env
    cfg.data["selector"]["source"]["provider"] = "hashrate_no"
    cfg.data["coins"]["XTM"]["miner"] = "peakminer"
    cfg.data["coins"]["XTM"]["pools"] = {"kryptex": "xtm.kryptex.network:7033"}
    cfg.data["coins"]["QTC"]["auto_switch"] = False                      # exercise the owner-approval gate
    monkeypatch.setattr(HashrateNoSource, "_fetch", staticmethod(fake_fetch([])))
    sel = Selector(cfg, db, clock=clock)
    sel.learn_perf("NVIDIA GeForce RTX 3080", "PRL", 125e12, 377)       # measured on the rig
    sel.learn_perf("NVIDIA GeForce RTX 3080", "XTM", 1.0, 192)
    sel.learn_perf("NVIDIA GeForce RTX 3080", "QTC", 488e6, 165)
    d = sel.decide(["NVIDIA GeForce RTX 3080"], "PRL")
    by = {e["coin"]: e for e in d["estimates"]}
    assert by["PRL"]["gross_usd_day"] == pytest.approx(0.0211 * 125) and by["PRL"]["source"] == "hashrate_no"
    assert by["QTC"]["gross_usd_day"] == pytest.approx(0.00346 * 488) and by["QTC"]["minable"]
    assert by["XTM"]["gross_usd_day"] == pytest.approx(2.6) and by["XTM"]["data_age_s"] == 0
    assert d["meta"]["hashrate_no_best"]["NVIDIA GeForce RTX 3080"]["profit"]["coin"] == "PRL"
    assert d["decision"] == "stay" and d["streak"]["coin"] is None      # XTM 2.6 vs PRL 2.64: no switch
