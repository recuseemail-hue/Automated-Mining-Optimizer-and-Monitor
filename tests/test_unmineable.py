"""Pool resolver (kryptex vs unMineable), unMineable stats client, price cache, miner login line."""
import pytest

from agent import service
from agent.miner.peakminer import PeakMiner
from agent.pool.resolve import pool_fee_pct, resolve_pool
from agent.pool.unmineable import UnmineableClient
from agent.prices import price_usd
from agent.selector import Selector

ADDR = {"success": True, "data": {"balance": "0.00012345", "balance_payable": "0.0001", "payment_threshold": "0.0005",
                                  "uuid": "abc-123", "auto_pay": True, "mining_fee": 1}}
WORKERS = {"success": True, "data": {"pearlpow": {"workers": [{"name": "rig1", "online": True, "chr": 101e12, "rhr": 100e12},
                                                               {"name": "laptop", "online": False, "chr": 0, "rhr": 0}]}}}


def test_resolver_kryptex_and_unmineable(cfg, monkeypatch):
    monkeypatch.setenv("RIG_WALLET_PRL", "prl1abc")
    monkeypatch.setenv("RIG_WALLET_BTC", "bc1qkraken")
    cfg.data["pool"]["provider"] = "kryptex"
    t = resolve_pool(cfg, "PRL", "rig1")
    assert t.url == "prl.kryptex.network:7048" and t.login == "prl1abc.rig1" and t.payout_coin == "PRL" and t.fee_pct == 1.0
    cfg.data["pool"]["provider"] = "unmineable"
    t = resolve_pool(cfg, "PRL", "rig1")
    assert t.url == "pearlpow.unmineable.com:3333" and t.login == "BTC:bc1qkraken.rig1" and t.payout_coin == "BTC"
    assert t.wallet_secret == "RIG_WALLET_BTC" and t.fee_pct == 1.0
    cfg.data["pool"]["unmineable"]["referral"] = "abcd-1234"
    t = resolve_pool(cfg, "PRL", "rig1")
    assert t.login.endswith("#abcd-1234") and pool_fee_pct(cfg) == 0.75
    # Quantus has no unMineable URL configured -> not minable under this provider
    q = resolve_pool(cfg, "QTC")
    assert q.url is None and "no unMineable pool URL" in q.note
    cfg.data["coins"]["QTC"]["pools"] = {"unmineable": "quantus.unmineable.com:3333"}
    assert resolve_pool(cfg, "QTC").url == "quantus.unmineable.com:3333"
    monkeypatch.delenv("RIG_WALLET_BTC")
    assert resolve_pool(cfg, "PRL").login is None


def test_peakminer_command_uses_unmineable_login(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_WALLET_BTC", "bc1qkraken")
    monkeypatch.setenv("PEAKMINER_API_KEY", "k")
    cfg.data["pool"]["provider"] = "unmineable"
    cfg.data["miner"]["exe"] = str(tmp_path / "peakminer.exe")
    cfg.data["miner"]["worker"] = "rig1"
    cmd = PeakMiner(cfg).command()
    assert cmd[cmd.index("-o") + 1] == "pearlpow.unmineable.com:3333" and cmd[cmd.index("-u") + 1] == "BTC:bc1qkraken.rig1"
    pm = PeakMiner(cfg)
    pm.coin_override = "QTC"
    with pytest.raises(RuntimeError):
        pm.command()


def test_unmineable_stats_and_cache(cfg, db, monkeypatch):
    monkeypatch.setenv("RIG_WALLET_BTC", "bc1qkraken")
    cfg.data["pool"]["provider"] = "unmineable"
    calls = []

    def fake_get(self, url):
        calls.append(url)
        if "/address/" in url:
            return ADDR
        if "/workers" in url:
            return WORKERS
        raise RuntimeError(url)
    monkeypatch.setattr(UnmineableClient, "_get", fake_get)
    monkeypatch.setattr("agent.pool.unmineable.price_usd", lambda sym, db=None: 100000.0)
    c = UnmineableClient(cfg, db)
    st = c.stats()
    assert calls[0].endswith("/address/bc1qkraken?coin=BTC") and calls[1].endswith("/account/abc-123/workers")
    assert st.balance == pytest.approx(0.00012345) and st.balance_usd == pytest.approx(12.345)
    assert st.workers == 1 and st.hashrate == 101e12 and st.extra["payout_coin"] == "BTC" and st.source == "live"
    # within TTL: served from cache without a call; after an outage: cached values with the error attached
    assert c.stats().source == "cache" and len(calls) == 2
    db.kv_set("pool.cache", {**db.kv_get("pool.cache"), "ts": 0})
    monkeypatch.setattr(UnmineableClient, "_get", lambda self, url: (_ for _ in ()).throw(RuntimeError("down")))
    st2 = c.stats()
    assert st2.source == "cache" and st2.balance == pytest.approx(0.00012345) and "down" in st2.error


def test_price_cache(db):
    n = {"calls": 0}

    def fetch(url):
        n["calls"] += 1
        return {"bitcoin": {"usd": 98765.4}}
    assert price_usd("BTC", db, fetch=fetch) == 98765.4
    assert price_usd("BTC", db, fetch=fetch) == 98765.4 and n["calls"] == 1
    assert price_usd("USDT", db, fetch=fetch) == 1.0
    assert price_usd("XYZ", db, fetch=fetch) is None
    db.kv_set("price.BTC", {"ts": 0, "usd": 90000.0})
    assert price_usd("BTC", db, fetch=lambda u: (_ for _ in ()).throw(RuntimeError("x"))) == 90000.0


def test_collector_records_payout_coin_and_switch_checks_pool(cfg, db, monkeypatch):
    monkeypatch.setenv("RIG_WALLET_BTC", "bc1qkraken")
    cfg.data["pool"]["provider"] = "unmineable"
    cfg.data["safety"]["dry_run"] = False
    monkeypatch.setattr(UnmineableClient, "_get", lambda self, url: ADDR if "/address/" in url else WORKERS)
    monkeypatch.setattr("agent.pool.unmineable.price_usd", lambda sym, db=None: 50000.0)
    st = service.build(cfg)
    st["collector"].collect_once()
    import json
    row = db.one("SELECT balance, balance_usd, extra FROM host_samples ORDER BY ts DESC LIMIT 1")
    assert row["balance"] == pytest.approx(0.00012345) and json.loads(row["extra"])["payout_coin"] == "BTC"
    from api import queries
    assert queries.overview(db)["balance"]["coin"] == "BTC"
    r = st["actions"].switch_coin("QTC", "claude", "t")                 # approved coin, but no unMineable URL
    assert not r.ok and "no unMineable pool URL" in r.error
    sel = Selector(cfg, db)
    ests, _ = sel.estimates(["RTX 3080"], "PRL")
    q = next(e for e in ests if e.coin == "QTC")
    assert q.minable is False and "unMineable" in q.note
