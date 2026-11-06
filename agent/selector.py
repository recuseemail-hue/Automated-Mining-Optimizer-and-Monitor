"""Algorithm/coin selection (deterministic part). Claude reviews the table this produces; the
switch itself only happens when the hysteresis rule here says so.

net_usd_day = gross * (1 - miner_dev_fee) * (1 - pool_fee) - power_kw * 24 * kwh_usd
Switching cost = downtime minutes of the *current* coin's revenue, charged once per switch.
Hysteresis: the best alternative must beat the current coin by selector.min_gain_pct on
selector.consecutive_checks consecutive checks, with data younger than selector.max_data_age_s.
Pearl fork: once PIP-3 has an activation height and the chain has passed it (or the miner reports
Pearl unminable), PRL is excluded for every GPU model not in selector.fork.minable_after_fork.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .db import Db
from .miner.base import UNIT_FACTORS
from .pool.kryptex import dig

log = logging.getLogger(__name__)
MINERSTAT_UNITS = {"H": 1.0, "KH": 1e3, "MH": 1e6, "GH": 1e9, "TH": 1e12}


@dataclass
class CoinEstimate:
    coin: str
    model: str
    hashrate: float | None
    power_w: float | None
    gross_usd_day: float | None
    net_usd_day: float | None
    source: str
    data_age_s: float | None
    minable: bool = True
    note: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Selector:
    def __init__(self, cfg: Config, db: Db, clock=time.time):
        self.cfg, self.db, self.clock = cfg, db, clock
        self.s = cfg.get("selector", {}) or {}
        self.catalog = cfg.get("coins", {}) or {}

    # --- profitability sources ---------------------------------------------------------------------
    def fetch_raw(self) -> Any:
        src = self.s.get("source", {}) or {}
        url = str(src.get("url", "")).replace("${COINS}", ",".join(self.catalog))
        req = urllib.request.Request(url, headers={"User-Agent": "rig-overseer/0.1 (+read-only profitability check)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def model_perf(self, model: str, coin: str) -> dict | None:
        """Expected hashrate/power for a GPU model on a coin: learned (kv) first, then the seed table."""
        learned = self.db.kv_get(f"perf.{model}.{coin}")
        if learned:
            return learned
        seed = self.s.get("seed", {}) or {}
        for needle, coins in seed.items():
            if needle == "as_of":
                continue
            if needle.lower() in (model or "").lower() and coin in coins:
                d = dict(coins[coin], seed=True)
                for k in ("hashrate", "power_w", "gross_usd_day"):      # YAML reads 105e12 as a string
                    if d.get(k) is not None:
                        d[k] = float(d[k])
                return d
        return None

    def learn_perf(self, model: str, coin: str, hashrate: float, power_w: float) -> None:
        self.db.kv_set(f"perf.{model}.{coin}", {"hashrate": hashrate, "power_w": power_w, "ts": self.clock()})

    def estimates(self, models: list[str], current_coin: str) -> tuple[list[CoinEstimate], dict]:
        src = self.s.get("source", {}) or {}
        provider = src.get("provider", "manual")
        from .pool.resolve import pool_fee_pct, resolve_pool
        fee_pool = pool_fee_pct(self.cfg) / 100
        kwh = float(self.cfg.get("economics.kwh_usd", 0.0))
        now = self.clock()
        live: dict[str, dict] = {}
        meta: dict[str, Any] = {"provider": provider, "verified": bool(src.get("verified", False)), "error": None}
        if provider == "http_json":
            try:
                raw = self.fetch_raw()
                f = src.get("fields", {}) or {}
                items = dig(raw, f.get("list")) if f.get("list") else raw
                for it in (items if isinstance(items, list) else []):
                    c = str(dig(it, f.get("coin")) or "").upper()
                    if c in self.catalog:
                        upd = dig(it, f.get("updated"))
                        live[c] = {"reward": dig(it, f.get("reward_per_unit_day")), "unit": dig(it, f.get("unit")),
                                   "price": dig(it, f.get("price_usd")), "age_s": (now - float(upd)) if upd else None}
                self.db.kv_set("selector.cache", {"ts": now, "live": live})
            except Exception as e:
                meta["error"] = str(e)[:200]
                cached = self.db.kv_get("selector.cache")
                if cached:
                    live = cached["live"]
                    for v in live.values():
                        v["age_s"] = (v.get("age_s") or 0) + (now - cached["ts"])
                    meta["from_cache"] = True
        out: list[CoinEstimate] = []
        seed_as_of = self.s.get("seed", {}).get("as_of")
        seed_age = None
        if seed_as_of:
            try:
                seed_age = now - time.mktime(time.strptime(str(seed_as_of), "%Y-%m-%d"))
            except ValueError:
                seed_age = None
        hn: dict[str, tuple[dict, dict]] = {}
        if provider == "hashrate_no":
            from .profit_sources import HashrateNoSource
            src_obj = HashrateNoSource(self.cfg, self.db, clock=self.clock)
            for model in models:
                hn[model] = src_obj.estimates(model, self.model_perf)
                if hn[model][1].get("error"):
                    meta["error"] = hn[model][1]["error"]
                best, _ = src_obj.gpu_best(model)
                if best:
                    meta.setdefault("hashrate_no_best", {})[model] = {k: best.get(k) for k in ("key", "profit", "revenue", "profit24")}
            meta["verified"] = bool(self.s.get("hashrate_no", {}).get("verified", src.get("verified", False)))
        for model in models:
            for coin, cat in self.catalog.items():
                perf = self.model_perf(model, coin)
                est_hn = (hn.get(model, ({}, {}))[0]).get(coin)
                if not perf and est_hn and est_hn.get("hashrate"):
                    perf = {"hashrate": est_hn["hashrate"], "power_w": est_hn.get("power_w"), "estimate": True}
                if not perf:
                    out.append(CoinEstimate(coin, model, None, None, None, None, "none", None, note="no expected hashrate for this model"))
                    continue
                hr, pw = perf.get("hashrate"), perf.get("power_w")
                gross, source, age = None, "seed", seed_age
                if est_hn and est_hn.get("usd_per_hs") is not None and hr:
                    hmeta = hn[model][1]
                    gross = float(est_hn["usd_per_hs"]) * float(hr)       # live $/H/s x this rig's expected hashrate
                    source = hmeta.get("source", "hashrate_no")
                    age = hmeta.get("age_s")
                elif coin in live and live[coin].get("reward") is not None and hr:
                    unit = MINERSTAT_UNITS.get(str(live[coin].get("unit") or "H").upper(), None) or UNIT_FACTORS.get(str(live[coin].get("unit")).lower(), 1.0)
                    coins_day = float(live[coin]["reward"]) * (hr / unit)
                    gross = coins_day * float(live[coin].get("price") or 0)
                    source, age = provider + ("(cache)" if meta.get("from_cache") else ""), live[coin].get("age_s")
                elif perf.get("gross_usd_day") is not None:
                    gross = float(perf["gross_usd_day"])
                net = None
                if gross is not None:
                    dev = float(cat.get("dev_fee_pct", 0)) / 100
                    net = gross * (1 - dev) * (1 - fee_pool) - (pw or 0) / 1000 * 24 * kwh
                est = CoinEstimate(coin, model, hr, pw, gross, net, source, age)
                if not self.miner_available(coin):
                    est.minable, est.note = False, f"no miner backend for {coin} (coins.{coin}.miner={cat.get('miner')})"
                elif not resolve_pool(self.cfg, coin).url:
                    est.minable, est.note = False, resolve_pool(self.cfg, coin).note
                elif not self.minable(coin, model):
                    est.minable, est.note = False, "not minable on this model (Pearl FP8 fork)"
                out.append(est)
        return out, meta

    # --- fork ----------------------------------------------------------------------------------------
    def fork_state(self) -> dict:
        st = self.db.kv_get("selector.fork") or {}
        cfg_h = self.s.get("fork", {}).get("activation_height")
        return {"activation_height": st.get("activation_height", cfg_h), "activated": bool(st.get("activated", False)),
                "note": st.get("note", ""), "updated": st.get("updated")}

    def set_fork(self, *, activation_height: int | None = None, activated: bool | None = None, note: str = "") -> dict:
        st = self.fork_state()
        if activation_height is not None:
            st["activation_height"] = activation_height
        if activated is not None:
            st["activated"] = activated
        st["note"], st["updated"] = note, self.clock()
        self.db.kv_set("selector.fork", st)
        return st

    AVAILABLE_MINERS = ("peakminer", "sim", "auto")

    def miner_available(self, coin: str) -> bool:
        return str(self.catalog.get(coin, {}).get("miner", "")) in self.AVAILABLE_MINERS

    def minable(self, coin: str, model: str) -> bool:
        from .pool.resolve import resolve_pool
        cat = self.catalog.get(coin, {})
        if not self.miner_available(coin) or not resolve_pool(self.cfg, coin).url:
            return False
        if not cat.get("fork_sensitive"):
            return True
        if not self.fork_state()["activated"]:
            return True
        ok = self.s.get("fork", {}).get("minable_after_fork", [])
        return any(n.lower() in (model or "").lower() for n in ok)

    # --- decision --------------------------------------------------------------------------------------
    def decide(self, models: list[str], current_coin: str, run_id: str | None = None) -> dict:
        ests, meta = self.estimates(models, current_coin)
        max_age = float(self.s.get("max_data_age_s", 21600))
        min_gain = float(self.s.get("min_gain_pct", 15))
        need = int(self.s.get("consecutive_checks", 2))
        downtime_min = float(self.cfg.get("economics.switch_downtime_min", 5))
        # aggregate per coin over the installed models (sum of net $/day across GPUs of those models)
        by_coin: dict[str, dict] = {}
        for e in ests:
            d = by_coin.setdefault(e.coin, {"net": 0.0, "minable": True, "fresh": True, "models": 0, "source": e.source})
            if e.net_usd_day is None:
                d["net"] = None if d["net"] is None else d["net"]
                continue
            if d["net"] is not None:
                d["net"] += e.net_usd_day
            d["models"] += 1
            d["minable"] &= e.minable
            d["fresh"] &= (e.data_age_s is not None and e.data_age_s <= max_age)
        cur = by_coin.get(current_coin, {"net": None, "minable": True, "fresh": False})
        cur_net = cur["net"] or 0.0
        forced = not cur["minable"]
        best_coin, best = None, None
        for coin, d in by_coin.items():
            if coin == current_coin or d["net"] is None or not d["minable"] or not d["fresh"]:
                continue
            if not self.catalog[coin].get("auto_switch", False):
                continue
            if best is None or d["net"] > best["net"]:
                best_coin, best = coin, d
        decision, reasoning = "stay", []
        streak_key = "selector.streak"
        streak = self.db.kv_get(streak_key) or {"coin": None, "n": 0}
        if forced:
            decision = f"switch:{best_coin}" if best_coin else "stop"
            reasoning.append(f"{current_coin} is no longer minable on this hardware (fork)")
            if not best_coin:
                reasoning.append("no fresh, auto-approved alternative: recommend owner action")
        elif best_coin is None:
            reasoning.append("no fresh, auto-approved alternative with data")
            streak = {"coin": None, "n": 0}
        else:
            gain = (best["net"] - cur_net) / cur_net * 100 if cur_net else float("inf")
            switch_cost = cur_net * downtime_min / 1440
            reasoning.append(f"best alternative {best_coin} net ${best['net']:.2f}/day vs {current_coin} ${cur_net:.2f}/day "
                             f"({gain:+.1f}%, switch cost ${switch_cost:.3f})")
            if gain >= min_gain and best["net"] - switch_cost > cur_net:
                streak = {"coin": best_coin, "n": streak["n"] + 1 if streak.get("coin") == best_coin else 1}
                if streak["n"] >= need:
                    decision = f"switch:{best_coin}"
                    reasoning.append(f"{streak['n']} consecutive checks >= {min_gain}%: switch")
                else:
                    reasoning.append(f"check {streak['n']}/{need} above threshold: hold for hysteresis")
            else:
                streak = {"coin": None, "n": 0}
                reasoning.append(f"below {min_gain}% threshold: stay")
            if not cur["fresh"] and cur_net:
                reasoning.append(f"note: current-coin data is older than {max_age / 3600:.0f} h")
        self.db.kv_set(streak_key, streak)
        ts = self.clock()
        for e in ests:
            self.db.execute("INSERT INTO coin_checks(ts, run_id, gpu_model, coin, gross_usd_day, net_usd_day, hashrate, power_w, source, "
                            "data_age_s, decision, reasoning) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ts, run_id, e.model, e.coin, e.gross_usd_day, e.net_usd_day, e.hashrate, e.power_w, e.source, e.data_age_s,
                             decision if e.coin in (current_coin, best_coin) else None, "; ".join(reasoning) if e.coin == current_coin else e.note))
        return {"ts": ts, "current": current_coin, "decision": decision, "target": best_coin if decision.startswith("switch") else None,
                "reasoning": reasoning, "streak": streak, "meta": meta, "fork": self.fork_state(),
                "estimates": [e.as_dict() for e in ests], "by_coin": by_coin}
