"""hashrate.no provider: parsing, caching, call cap, selector integration."""
import pytest

from agent import service
from agent.profit_sources import HashrateNoSource, gpu_key
from agent.selector import Selector


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


SAMPLE = {"gpu": "3080", "estimates": [
    {"coin": "PRL", "algorithm": "pearl", "hashrate": 105, "hashrateUnit": "TH/s", "power": 240, "revenueUSD": 2.11, "updated": 1_800_000_000 - 600},
    {"coin": "XTM", "algorithm": "cuckaroo29", "hashrate": 1.0, "hashrateUnit": "H/s", "power": 192, "revenueUSD": 2.60},
    {"coin": "QTC", "algorithm": "quantus", "hashrate": 488, "hashrateUnit": "MH/s", "power": 165, "revenueUSD": 3.00},
    {"coin": "DOGE", "hashrate": 1, "revenueUSD": 0.1},
]}


def test_gpu_key():
    assert gpu_key("NVIDIA GeForce RTX 3080") == "3080"
    assert gpu_key("NVIDIA GeForce RTX 3080 Ti") == "3080Ti" and gpu_key("RTX 4070 SUPER") == "4070Super"
    assert gpu_key("NVIDIA GeForce RTX 3080 (sim)", {"3080 (sim)": "3080"}) == "3080"
    assert gpu_key("Radeon RX 6800") is None


@pytest.fixture
def env(cfg, db, monkeypatch):
    monkeypatch.setenv("HASHRATE_NO_KEY", "hn-key-123456")
    cfg.data["safety"]["dry_run"] = False
    cfg.data["notify"]["provider"] = "none"
    clock = Clock()
    st = service.build(cfg, clock=clock)
    st["collector"].register_gpus()
    return cfg, db, st, clock


def test_parse_cache_and_cap(env):
    cfg, db, st, clock = env
    calls = []

    def fetch(url, headers):
        calls.append(url)
        return SAMPLE
    src = HashrateNoSource(cfg, db, clock=clock, fetch=fetch)
    data, meta = src.estimates("NVIDIA GeForce RTX 3080")
    assert "apiKey=hn-key-123456" in calls[0] and "gpu=3080" in calls[0]
    assert data["PRL"]["hashrate"] == 105e12 and data["QTC"]["hashrate"] == 488e6 and data["XTM"]["power_w"] == 192
    assert data["PRL"]["updated"] == 1_800_000_000 - 600 and meta["source"] == "hashrate_no"
    # second call inside the TTL hits the cache; after the TTL it refetches
    src.estimates("NVIDIA GeForce RTX 3080")
    assert len(calls) == 1
    clock.t += 7 * 3600
    src.estimates("NVIDIA GeForce RTX 3080")
    assert len(calls) == 2
    # monthly cap: further live calls refused, cache still served
    cfg.data["selector"]["hashrate_no"]["monthly_call_cap"] = 2
    src = HashrateNoSource(cfg, db, clock=clock, fetch=fetch)
    clock.t += 7 * 3600
    data, meta = src.estimates("NVIDIA GeForce RTX 3080")
    assert len(calls) == 2 and meta["source"] == "hashrate_no(cache)" and "cap" in meta["error"]
    # an unmappable model never burns a call
    assert HashrateNoSource(cfg, db, clock=clock, fetch=fetch).estimates("Radeon RX 6800")[1]["source"] == "unavailable"
    # dict-shaped response also parses
    alt = {"PRL": {"hashrate": "105", "hashrateUnit": "TH/s", "revenueUSD": "2.1"}}
    assert src.parse_gpu(alt)["PRL"]["hashrate"] == 105e12


def test_selector_uses_hashrate_no_and_scales_by_learned_hashrate(env, monkeypatch):
    cfg, db, st, clock = env
    cfg.data["selector"]["source"]["provider"] = "hashrate_no"
    cfg.data["coins"]["XTM"]["miner"] = "peakminer"           # pretend a Tari backend exists for this test
    cfg.data["coins"]["QTC"]["auto_switch"] = False           # this test checks the owner-approval gate
    monkeypatch.setattr(HashrateNoSource, "_fetch", staticmethod(lambda url, headers: SAMPLE))
    sel = Selector(cfg, db, clock=clock)
    d = sel.decide(["NVIDIA GeForce RTX 3080"], "PRL")
    by = {e["coin"]: e for e in d["estimates"]}
    assert by["PRL"]["gross_usd_day"] == pytest.approx(2.11) and by["PRL"]["source"] == "hashrate_no"
    assert by["PRL"]["data_age_s"] == pytest.approx(600)
    assert by["XTM"]["net_usd_day"] > by["PRL"]["net_usd_day"]          # +23 % -> first check holds for hysteresis
    assert d["decision"] == "stay" and d["streak"] == {"coin": "XTM", "n": 1}
    assert by["QTC"]["net_usd_day"] > by["XTM"]["net_usd_day"] and d["target"] is None   # QTC needs owner approval
    assert "DOGE" not in by                                              # not in the catalog
    # our real PRL hashrate is 10 % lower than hashrate.no's estimate -> revenue scaled down
    sel.learn_perf("NVIDIA GeForce RTX 3080", "PRL", 94.5e12, 240)
    d2 = sel.decide(["NVIDIA GeForce RTX 3080"], "PRL")
    by2 = {e["coin"]: e for e in d2["estimates"]}
    assert by2["PRL"]["gross_usd_day"] == pytest.approx(2.11 * 0.9)
    assert d2["decision"] == "switch:XTM"                                 # second consecutive check above 15 %
