"""rigctl: command-line entry point for operators and for Claude (via the allowlist in M2)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from . import __version__, secrets
from .config import load_config
from .db import Db
from .log import setup_logging


def _ctx(args):
    cfg = load_config(args.config)
    secrets.configure(cfg.secrets_file)
    setup_logging(cfg.data_dir, "rigctl", cfg.get("logging.level", "INFO"))
    return cfg


def _stack(cfg):
    from .collector import Collector
    from .gpu import make_backend, with_control_router
    from .miner import make_miner
    from .platform import get_platform
    from .pool import make_pool

    db = Db(cfg.db_path)
    gpu = make_backend(cfg)
    miner = make_miner(cfg, gpu)
    gpu = with_control_router(gpu, miner, cfg)
    plat = get_platform(cfg)
    pool = make_pool(cfg, db)
    return db, Collector(cfg, db, gpu, miner, plat, pool)


def cmd_init_db(args):
    cfg = _ctx(args)
    db = Db(cfg.db_path)
    print(f"db ready: {cfg.db_path} (schema v{db.one('SELECT version FROM schema_version')['version']})")


def cmd_config(args):
    cfg = _ctx(args)
    from .config import validate
    problems = validate(cfg)
    print("sources:", ", ".join(str(p) for p in cfg.sources))
    for pr in problems:
        print("  !", pr)
    if args.check:
        print("config OK" if not [x for x in problems if not x.startswith("note:")] else "config has problems")
        return 0 if not [x for x in problems if not x.startswith("note:")] else 2
    print(f"host={cfg.host_name} role={cfg.role} data_dir={cfg.data_dir} db={cfg.db_path} dry_run={cfg.dry_run}")
    print(f"gpu.backend={cfg.get('gpu.backend')} miner.backend={cfg.get('miner.backend')} pool={cfg.get('pool.provider')}")
    unresolved = [s for s in json.dumps(cfg.data).split('"') if s.startswith("${")]
    if unresolved:
        print("unresolved ${ENV} references:", ", ".join(sorted(set(unresolved))))
    for g in cfg.get("gpus", []) or []:
        print(f"  gpu {g.get('uuid')} control_enabled={g.get('control_enabled', False)}")


def cmd_gpus(args):
    cfg = _ctx(args)
    from .gpu import make_backend, with_control_router
    be = make_backend(cfg)
    for i in be.enumerate():
        s = be.sample(i.uuid)
        print(f"[{i.index}] {i.name} {i.uuid} bus={i.bus_id} pl={s.pl_w}/{i.stock_pl_w}W "
              f"{s.power_w and round(s.power_w)}W {s.core_c}C memj={s.memj_c} fan={s.fan_pct}% "
              f"core={s.core_mhz} mem={s.mem_mhz} off=({s.core_offset_mhz},{s.mem_offset_mhz}) "
              f"thr={','.join(s.throttle) or '-'} ctl={'on' if cfg.gpu_control_enabled(i.uuid) else 'off'}")
    be.close()


def cmd_sensors(args):
    """Report what this host can and cannot read/control. Run early on every new rig."""
    cfg = _ctx(args)
    from .gpu import make_backend, with_control_router
    from .platform import get_platform
    plat = get_platform(cfg)
    be = make_backend(cfg)
    print(f"platform={plat.name} admin={plat.is_admin()} gpu_backend={be.name}")
    hs = plat.sample()
    print(f"host: cpu_c={hs.cpu_c} cpu_w={hs.cpu_w} cpu_pct={hs.cpu_pct} ram={hs.ram_used_mb and round(hs.ram_used_mb)}/"
          f"{hs.ram_total_mb and round(hs.ram_total_mb)}MB")
    for i in be.enumerate():
        print(f"[{i.index}] {i.name}")
        for k, v in be.capabilities(i.uuid).items():
            print(f"    {k}: {v}")
        if args.probe_write:
            if not cfg.gpu_control_enabled(i.uuid):
                print("    probe_write: skipped (control_enabled=false for this GPU)")
            elif hasattr(be, "probe_write"):
                for k, v in be.probe_write(i.uuid).items():
                    print(f"    write {k}: {v}")
    notes = plat.sensor_notes()
    if notes:
        print("not exposed on this host:")
        for k, v in notes.items():
            print(f"    {k}: {v}")
    be.close()


def cmd_collect(args):
    cfg = _ctx(args)
    db, col = _stack(cfg)
    if args.loop:
        threads = []
        if cfg.role == "worker":
            import threading
            from .forwarder import Forwarder
            fw = Forwarder(cfg, db)
            t = threading.Thread(target=fw.run_loop, name="forwarder", daemon=True)
            t.start()
            threads.append(t)
        try:
            col.run_loop(args.interval)
        except KeyboardInterrupt:
            col.stop()
    else:
        print(json.dumps(col.collect_once(), default=str))


def cmd_status(args):
    cfg = _ctx(args)
    db = Db(cfg.db_path)
    host = db.one("SELECT * FROM hosts WHERE name=?", (cfg.host_name,))
    if not host:
        print("no samples yet for this host; run `rigctl collect`")
        return
    hs = db.one("SELECT * FROM host_samples WHERE host_id=? AND res='5m' ORDER BY ts DESC LIMIT 1", (host["id"],))
    if hs:
        age = time.time() - hs["ts"]
        print(f"host {host['name']} ({host['role']}): last sample {age:.0f}s ago miner={hs['miner_state']} "
              f"coin={hs['coin']} hr={_fmt_hr(hs['miner_hashrate'])} gpu_w={hs['gpu_w_total'] and round(hs['gpu_w_total'])} "
              f"cpu_c={hs['cpu_c']} ram={hs['ram_used_mb'] and round(hs['ram_used_mb'])}MB balance={hs['balance']} "
              f"({hs['balance_usd']} USD)")
    for g in db.gpus_for_host(host["id"]):
        s = db.one("SELECT * FROM gpu_samples WHERE gpu_id=? AND res='5m' ORDER BY ts DESC LIMIT 1", (g["id"],))
        if s:
            print(f"  [{g['idx']}] {g['model']}: {_fmt_hr(s['hashrate'])} {s['power_w'] and round(s['power_w'])}W "
                  f"{s['core_c']}C memj={s['memj_c']} fan={s['fan_pct']}% pl={s['pl_w']}W acc/rej={s['accepted']}/{s['rejected']} "
                  f"thr={s['throttle'] or '-'} ctl={'on' if g['control_enabled'] else 'off'}")
    n = db.one("SELECT COUNT(*) c FROM outbox")["c"]
    if n:
        print(f"  outbox: {n} rows waiting for hub")


def cmd_miner(args):
    cfg = _ctx(args)
    from .gpu import make_backend, with_control_router
    from .miner import make_miner
    gpu = make_backend(cfg)
    m = make_miner(cfg, gpu)
    if args.sub == "dump":
        if hasattr(m, "fetch_raw"):
            print(json.dumps(m.fetch_raw(), indent=1))
        else:
            print(json.dumps(asdict(m.stats()), indent=1, default=str))
    else:
        st = m.stats()
        print(f"state={st.state} coin={st.coin} algo={st.algo} pool={st.pool} hr={_fmt_hr(st.total_hashrate)} "
              f"acc/rej={st.accepted}/{st.rejected} uptime={st.uptime_s} err={st.error}")
        for g in st.gpus:
            print(f"  gpu{g.index} bus={g.bus_id} {_fmt_hr(g.hashrate)} acc/rej={g.accepted}/{g.rejected} "
                  f"temp={g.temp_c} memj={g.memj_c} paused={g.paused}")
    gpu.close()


def cmd_pool(args):
    cfg = _ctx(args)
    from .pool import make_pool
    p = make_pool(cfg, Db(cfg.db_path))
    if args.sub == "probe":
        print(json.dumps(p.probe(), indent=1)[:6000])
    else:
        st = p.stats()
        print(f"source={st.source} verified={st.verified} hr={_fmt_hr(st.hashrate)} workers={st.workers} "
              f"balance={st.balance} usd={st.balance_usd} err={st.error}")


def cmd_sim(args):
    """Fault injection for the simulator (tests/watchdog verification)."""
    cfg = _ctx(args)
    from .gpu.sim import SimBackend
    be = SimBackend(cfg.get("gpu.sim.state_file") or (cfg.data_dir / "sim_gpus.json"), int(cfg.get("gpu.sim.count", 2)))
    if args.clear:
        be.clear_faults(args.uuid)
        print("faults cleared")
        return
    faults = {}
    for kv in args.set or []:
        k, v = kv.split("=", 1)
        faults[k] = json.loads(v) if v not in ("", "null") else None
    be.inject(args.uuid, **faults)
    print("faults:", be._gpu(args.uuid).get("faults") if not faults.get("missing") else {"missing": True})


# --- M2: service, actions, profiles, trials, incidents ----------------------------------------
def _live(args):
    cfg = _ctx(args)
    from . import service
    return cfg, service.build(cfg)


def cmd_agent(args):
    cfg = _ctx(args)
    from . import service
    service.run(cfg)


def cmd_act(args):
    """Allowlisted control actions. This is the only hardware interface exposed to Claude runs."""
    cfg, st = _live(args)
    a = st["actions"]
    actor, reason, rid = args.actor, args.reason, args.run_id
    k = args.what
    if k == "pl":
        r = a.set_power_limit(args.gpu, float(args.value), actor, reason, run_id=rid)
    elif k == "core-offset":
        r = a.set_core_offset(args.gpu, int(args.value), actor, reason, run_id=rid)
    elif k == "mem-offset":
        r = a.set_mem_offset(args.gpu, int(args.value), actor, reason, run_id=rid)
    elif k == "core-lock":
        r = a.set_core_lock(args.gpu, int(args.value), actor, reason, run_id=rid) if args.value not in (None, "reset") \
            else a.reset_core_lock(args.gpu, actor, reason, run_id=rid)
    elif k == "mem-lock":
        r = a.set_mem_lock(args.gpu, int(args.value), actor, reason, run_id=rid) if args.value not in (None, "reset") \
            else a.reset_mem_lock(args.gpu, actor, reason, run_id=rid)
    elif k == "fan":
        r = a.set_fan(args.gpu, None if args.value in (None, "auto") else int(args.value), actor, reason, run_id=rid)
    elif k == "miner-start":
        r = a.miner_start(actor, reason, run_id=rid)
    elif k == "miner-stop":
        r = a.miner_stop(actor, reason, run_id=rid)
    elif k == "miner-restart":
        r = a.miner_restart(actor, reason, run_id=rid)
    elif k == "pause":
        r = a.miner_pause_gpu(args.gpu, actor, reason, run_id=rid)
    elif k == "resume":
        r = a.miner_resume_gpu(args.gpu, actor, reason, run_id=rid)
    elif k == "switch-coin":
        r = a.switch_coin(args.value, actor, reason, run_id=rid)
    elif k == "reboot":
        r = a.reboot(actor, reason, run_id=rid)
    elif k == "shutdown":
        r = a.shutdown(actor, reason, run_id=rid)
    else:
        raise SystemExit(f"unknown action {k}")
    print(json.dumps(r.as_dict(), default=str))
    return 0 if r.ok else 2


def cmd_profile(args):
    cfg, st = _live(args)
    db, a, tr = st["db"], st["actions"], st["trials"]
    uuid = a.resolve(args.gpu)
    gid = db.gpu_id(uuid)
    if args.sub == "show":
        print(json.dumps({"current": a.settings(uuid), "active": db.profile(gid, "active"),
                          "known_good": db.profile(gid, "known_good"), "converged": db.profile(gid, "converged")},
                         indent=1, default=str))
    elif args.sub == "save-current":
        pid = db.save_profile(gid, a.settings(uuid), args.status, created_by=args.actor, note=args.reason)
        print(f"saved profile {pid} as {args.status}")
    elif args.sub == "restore":
        prof = db.profile(gid, args.status)
        if not prof:
            raise SystemExit(f"no {args.status} profile for {uuid}")
        res = a.apply_settings(uuid, prof["settings"], args.actor, args.reason or f"restore {args.status}")
        print(json.dumps([r.as_dict() for r in res], default=str))


def cmd_trial(args):
    cfg, st = _live(args)
    db, tr = st["db"], st["trials"]
    if args.sub == "list":
        for t in db.open_trials():
            print(json.dumps(t, default=str))
    elif args.sub == "start":
        setting = {args.key: (None if args.value == "null" else float(args.value) if "." in args.value else int(args.value))}
        uuid = st["actions"].resolve(args.gpu)
        print(json.dumps(tr.start(uuid, setting, float(args.hold), actor=args.actor, reason=args.reason, run_id=args.run_id,
                                  kind=args.kind), default=str))
    elif args.sub == "abort":
        print(f"aborted {tr.abort_all(args.reason or 'operator abort', actor=args.actor)} trial(s)")
    elif args.sub == "tick":
        print(json.dumps(tr.tick(), default=str))


def cmd_web(args):
    cfg = _ctx(args)
    from api.app import serve
    serve(cfg)


def cmd_optimize(args):
    """Time-boxed per-GPU optimization. One GPU at a time; every step is a self-reverting trial."""
    cfg, st = _live(args)
    from .optimizer import Optimizer, format_report
    o = cfg.data.setdefault("optimize", {})
    if args.window_min:
        o["per_gpu_s"] = args.window_min * 60
    if args.cap_min:
        o["rig_cap_s"] = args.cap_min * 60
    if args.hold_min:
        o["hold_s"] = args.hold_min * 60
    if args.steps:
        o["steps"] = args.steps
    opt = Optimizer(cfg, st["db"], st["actions"], st["trials"], st["collector"])
    a = st["actions"]
    uuids = [i.uuid for i in st["gpu"].enumerate()] if args.gpu in (None, "all") else [a.resolve(args.gpu)]
    uuids = [u for u in uuids if cfg.gpu_control_enabled(u)] if args.gpu in (None, "all") else uuids
    if not uuids:
        raise SystemExit("no GPU with control_enabled=true")
    print(f"optimize run {opt.run_id}: gpus={uuids} goal={args.goal or cfg.get('optimize.default_goal')} "
          f"window={opt.per_gpu_s / 60:.0f}min cap={opt.rig_cap_s / 60:.0f}min hold={opt.hold_s / 60:.0f}min dry_run={cfg.dry_run}")
    reports = opt.run(uuids, args.goal)
    print(format_report(reports))
    if args.json:
        print(json.dumps([r.as_dict() for r in reports], default=str))
    return 0 if all(r.outcome in ("applied", "restored") for r in reports) else 2


# --- M5: scheduled Claude tasks -------------------------------------------------------------------
def cmd_tune(args):
    cfg, st = _live(args)
    from .tuner import Tuner
    t = Tuner(cfg, st["db"], st["actions"])
    coin = st["miner"].active_coin()["coin"]
    print(json.dumps(t.precheck(coin), indent=1 if args.pretty else None, default=str))


def cmd_coins(args):
    cfg, st = _live(args)
    from .selector import Selector
    sel = Selector(cfg, st["db"])
    if args.sub == "probe":
        if cfg.get("selector.source.provider") == "hashrate_no":
            from .profit_sources import HashrateNoSource
            models = sorted({g["model"] for g in st["db"].gpus_for_host(st["collector"].host_id)}) or ["NVIDIA GeForce RTX 3080"]
            print(json.dumps(HashrateNoSource(cfg, st["db"]).probe(models[0]), indent=1, default=str)[:12000])
        else:
            print(json.dumps(sel.fetch_raw(), indent=1)[:8000])
        return
    if args.sub == "fork":
        print(json.dumps(sel.set_fork(activation_height=args.height, activated=True if args.activated else None, note=args.note or ""),
                         default=str))
        return
    models = sorted({g["model"] for g in st["db"].gpus_for_host(st["collector"].host_id)}) or ["RTX 3080"]
    coin = st["miner"].active_coin()["coin"]
    d = sel.decide(models, coin, run_id=args.run_id)
    if args.sub == "table":
        print(f"current={d['current']} decision={d['decision']} source={d['meta']['provider']} verified={d['meta']['verified']} "
              f"err={d['meta']['error']}")
        for r in d["reasoning"]:
            print("  " + r)
        print(f"  {'model':12} {'coin':5} {'gross$/d':>9} {'net$/d':>8} {'age':>8} {'src':8} minable")
        for e in d["estimates"]:
            age = "-" if e["data_age_s"] is None else f"{e['data_age_s'] / 3600:.1f}h"
            g = "-" if e["gross_usd_day"] is None else f"{e['gross_usd_day']:.2f}"
            n = "-" if e["net_usd_day"] is None else f"{e['net_usd_day']:.2f}"
            print(f"  {e['model'][:12]:12} {e['coin']:5} {g:>9} {n:>8} {age:>8} {e['source']:8} {e['minable']} {e['note']}")
    else:
        print(json.dumps(d, default=str))


def cmd_claude_run(args):
    cfg, st = _live(args)
    from .claude_runner import ClaudeRunner
    from .context import build_context
    ctx = build_context(cfg, st, args.task, incident_id=args.incident)
    runner = ClaudeRunner(cfg, st["db"])
    if args.dry:
        d = runner.run_task(args.task, args.trigger, ctx, schedule=args.schedule, dry=True)
        print("$ " + " ".join(f"'{a}'" if " " in a else a for a in d["args"]))
        print(d["prompt"])
        return
    gating = bool(cfg.get("claude.hourly_gating", True))
    if args.task == "hourly" and gating and not ctx.get("candidates"):
        run_id = f"hourly-{time.strftime('%Y%m%d-%H%M')}-gated"
        st["db"].execute("INSERT INTO claude_runs(run_id, task, trigger, schedule, started, ended, status, summary, output) "
                         "VALUES(?,?,?,?,?,?,'skipped',?,?)",
                         (run_id, "hourly", args.trigger, args.schedule, time.time(), time.time(),
                          "no tuning candidates; LLM call skipped (hourly_gating)", json.dumps(ctx, default=str)))
        print(json.dumps({"run_id": run_id, "status": "skipped", "summary": "no candidates; gated"}))
        return
    res = runner.run_task(args.task, args.trigger, ctx, schedule=args.schedule)
    if args.task == "incident" and args.incident:
        st["db"].update_incident(args.incident, claude_run_id=res.get("run_id"))
        st["notifier"].push(f"{cfg.host_name}: Claude on incident #{args.incident} ({res.get('status')})",
                            (res.get("summary") or "")[:400], priority="high", tags="robot")
    print(json.dumps(res, default=str))
    return 0 if res.get("status") in ("ok", "skipped") else 2


def cmd_schedule(args):
    cfg = _ctx(args)
    from .scheduler import install, plan
    if args.sub == "show":
        for p in plan(cfg):
            print(json.dumps(p))
    else:
        for line in install(cfg, apply=(args.sub == "install")):
            print(line)


# --- M7: services, secrets, sysadmin helpers -------------------------------------------------------
def cmd_service(args):
    cfg = _ctx(args)
    from . import services
    if args.sub in ("plan", "install"):
        lines = services.install(cfg, apply=(args.sub == "install"))
    elif args.sub == "uninstall":
        lines = services.uninstall(cfg)
    else:
        lines = services.status(cfg)
    print("\n".join(lines))


def cmd_footprint(args):
    _ctx(args)
    from .services import footprint
    rows = footprint()
    tot = sum(r["rss_mb"] for r in rows)
    for r in rows:
        print(f"{r['pid']:>7} {r['rss_mb']:>8.1f} MB {r['cpu_pct']:>5.1f}%  {r['cmd']}")
    print(f"total rig-overseer RSS: {tot:.0f} MB (budget ~1024 MB)")


def cmd_secret(args):
    cfg = _ctx(args)
    from .secrets import get_secret, set_secret_store
    if args.sub == "set":
        import getpass
        val = args.value or getpass.getpass(f"{args.name}: ")
        print(f"{args.name} stored in {set_secret_store(args.name, val)}")
    else:
        names = ["RIG_WALLET_PRL", "RIG_WALLET_XTM", "RIG_WALLET_CFX", "NTFY_TOPIC", "NTFY_TOKEN", "RIG_HUB_TOKEN",
                 "DASHBOARD_PASSWORD", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "HEALTHCHECKS_URL"]
        for n in names:
            print(f"{n:24} {'set' if get_secret(n) else '-'}")


def cmd_sys(args):
    cfg = _ctx(args)
    from . import sysadmin
    if args.sub == "restore-point":
        print("ok" if sysadmin.restore_point(cfg, args.desc) else "failed (see log)")
    else:
        print(sysadmin.log_change(cfg, args.who, args.desc, args.undo or "see change description"))


def cmd_incidents(args):
    cfg = _ctx(args)
    db = Db(cfg.db_path)
    for r in db.query("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (args.limit,)):
        print(json.dumps(dict(r), default=str))


def cmd_actions(args):
    cfg = _ctx(args)
    db = Db(cfg.db_path)
    for r in db.query("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (args.limit,)):
        print(json.dumps(dict(r), default=str))


def _fmt_hr(h):
    if h is None:
        return "-"
    for unit, f in (("TH/s", 1e12), ("GH/s", 1e9), ("MH/s", 1e6), ("kH/s", 1e3)):
        if h >= f:
            return f"{h / f:.2f} {unit}"
    return f"{h:.0f} H/s"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rigctl", description=f"rig-overseer {__version__}")
    p.add_argument("--config", help="path to config.yaml (default: repo config.yaml + config.local.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create/migrate the database").set_defaults(fn=cmd_init_db)
    cf = sub.add_parser("config", help="show effective config, problems and unresolved secrets")
    cf.add_argument("--check", action="store_true", help="exit 2 if the config has problems")
    cf.set_defaults(fn=cmd_config)
    sub.add_parser("gpus", help="list GPUs with a live sample").set_defaults(fn=cmd_gpus)
    s = sub.add_parser("sensors", help="what this host can read/control; run on every new rig")
    s.add_argument("--probe-write", action="store_true", help="no-op writes to prove control works (needs control_enabled)")
    s.set_defaults(fn=cmd_sensors)
    c = sub.add_parser("collect", help="take one sample (default) or run the 5-minute loop")
    c.add_argument("--loop", action="store_true")
    c.add_argument("--interval", type=float, help="seconds (default collector.interval_s)")
    c.set_defaults(fn=cmd_collect)
    sub.add_parser("status", help="latest stored sample per GPU").set_defaults(fn=cmd_status)
    m = sub.add_parser("miner", help="miner stats; `miner dump` prints the raw API JSON")
    m.add_argument("sub", nargs="?", default="stats", choices=["stats", "dump"])
    m.set_defaults(fn=cmd_miner)
    po = sub.add_parser("pool", help="pool stats; `pool probe` prints raw JSON to verify field mapping")
    po.add_argument("sub", nargs="?", default="stats", choices=["stats", "probe"])
    po.set_defaults(fn=cmd_pool)
    si = sub.add_parser("sim", help="inject faults into the simulated GPUs")
    si.add_argument("uuid")
    si.add_argument("--set", action="append", metavar="KEY=JSON", help="e.g. core_c=92 fan_rpm=0 missing=true")
    si.add_argument("--clear", action="store_true")
    si.set_defaults(fn=cmd_sim)
    sub.add_parser("agent", help="run the agent service (watchdog + collector [+ forwarder]) in the foreground").set_defaults(fn=cmd_agent)
    ac = sub.add_parser("act", help="allowlisted control action (clamped, logged)")
    ac.add_argument("what", choices=["pl", "core-offset", "mem-offset", "core-lock", "mem-lock", "fan", "miner-start", "miner-stop",
                                     "miner-restart", "pause", "resume", "switch-coin", "reboot", "shutdown"])
    ac.add_argument("value", nargs="?", help="watts | MHz | pct|auto | coin")
    ac.add_argument("--gpu", help="GPU uuid or index")
    ac.add_argument("--actor", default="user", choices=["user", "claude", "rule", "optimizer"])
    ac.add_argument("--reason", required=True)
    ac.add_argument("--run-id")
    ac.set_defaults(fn=cmd_act)
    pr = sub.add_parser("profile", help="per-GPU setting profiles")
    pr.add_argument("sub", choices=["show", "save-current", "restore"])
    pr.add_argument("--gpu", required=True)
    pr.add_argument("--status", default="known_good", choices=["active", "known_good"])
    pr.add_argument("--actor", default="user")
    pr.add_argument("--reason", default="")
    pr.set_defaults(fn=cmd_profile)
    tr = sub.add_parser("trial", help="bounded, self-reverting setting trials")
    tr.add_argument("sub", choices=["list", "start", "abort", "tick"])
    tr.add_argument("--gpu")
    tr.add_argument("--key", choices=["pl_w", "core_offset_mhz", "mem_offset_mhz", "core_lock_mhz", "mem_lock_mhz", "fan_pct"])
    tr.add_argument("--value")
    tr.add_argument("--hold", default="900", help="seconds (default 900)")
    tr.add_argument("--kind", default="hourly", choices=["hourly", "optimize"])
    tr.add_argument("--actor", default="user")
    tr.add_argument("--reason", default="")
    tr.add_argument("--run-id")
    tr.set_defaults(fn=cmd_trial)
    sub.add_parser("web", help="run the hub API + dashboard server (hub role only)").set_defaults(fn=cmd_web)
    op = sub.add_parser("optimize", help="time-boxed per-GPU optimization (A efficiency, B max-hashrate, C balanced)")
    op.add_argument("--gpu", default="all", help="GPU uuid or index, or all (control-enabled GPUs only)")
    op.add_argument("--goal", choices=["max-hashrate", "max-efficiency", "target-temp"])
    op.add_argument("--window-min", type=float, help="hard window per GPU (default optimize.per_gpu_s)")
    op.add_argument("--cap-min", type=float, help="rig-wide cap (default optimize.rig_cap_s)")
    op.add_argument("--hold-min", type=float, help="hold per step (default optimize.hold_s)")
    op.add_argument("--steps", type=int, help="max steps per strategy (default optimize.steps)")
    op.add_argument("--json", action="store_true", help="also print the JSON report")
    op.set_defaults(fn=cmd_optimize)
    tu = sub.add_parser("tune", help="hourly deterministic pre-check: candidates, triggers, proposal (JSON)")
    tu.add_argument("--pretty", action="store_true")
    tu.set_defaults(fn=cmd_tune)
    co = sub.add_parser("coins", help="profitability table / decision; `coins probe` raw source JSON; `coins fork` record fork status")
    co.add_argument("sub", nargs="?", default="table", choices=["table", "json", "probe", "fork"])
    co.add_argument("--height", type=int, help="fork: announced activation height")
    co.add_argument("--activated", action="store_true", help="fork: Pearl no longer minable on this rig")
    co.add_argument("--note")
    co.add_argument("--run-id")
    co.set_defaults(fn=cmd_coins)
    cr = sub.add_parser("claude-run", help="run a scheduled Claude task headlessly (hub)")
    cr.add_argument("task", choices=["hourly", "algo_check", "incident"])
    cr.add_argument("--trigger", default="manual", choices=["schedule", "manual", "watchdog"])
    cr.add_argument("--schedule", help="cron string for the dashboard row")
    cr.add_argument("--incident", type=int, help="incident id (incident task)")
    cr.add_argument("--dry", action="store_true", help="print the command and prompt; do not call Claude")
    cr.set_defaults(fn=cmd_claude_run)
    sc = sub.add_parser("schedule", help="register the hourly/algo-check runs with the OS scheduler")
    sc.add_argument("sub", nargs="?", default="show", choices=["show", "plan", "install"])
    sc.set_defaults(fn=cmd_schedule)
    sv = sub.add_parser("service", help="auto-start services: agent (all hosts) + web (hub)")
    sv.add_argument("sub", nargs="?", default="status", choices=["plan", "install", "uninstall", "status"])
    sv.set_defaults(fn=cmd_service)
    sub.add_parser("footprint", help="RSS/CPU of rig-overseer processes").set_defaults(fn=cmd_footprint)
    se = sub.add_parser("secret", help="store a secret (Credential Manager on Windows, secrets file elsewhere) or list which are set")
    se.add_argument("sub", choices=["set", "list"])
    se.add_argument("name", nargs="?")
    se.add_argument("value", nargs="?")
    se.set_defaults(fn=cmd_secret)
    sy = sub.add_parser("sys", help="system-change helpers for incident runs: restore-point, log-change")
    sy.add_argument("sub", choices=["restore-point", "log-change"])
    sy.add_argument("desc")
    sy.add_argument("--undo")
    sy.add_argument("--who", default="claude")
    sy.set_defaults(fn=cmd_sys)
    inc = sub.add_parser("incidents", help="recent incidents")
    inc.add_argument("--limit", type=int, default=20)
    inc.set_defaults(fn=cmd_incidents)
    al = sub.add_parser("actions", help="recent action log rows")
    al.add_argument("--limit", type=int, default=30)
    al.set_defaults(fn=cmd_actions)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
        return int(rc or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {secrets.redact(str(e))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
