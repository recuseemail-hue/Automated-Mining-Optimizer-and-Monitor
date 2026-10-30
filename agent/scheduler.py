"""Register scheduled Claude runs with the OS scheduler on the hub.

Windows: Task Scheduler (`schtasks`), running `rigctl claude-run <task>` as SYSTEM, hidden, with the
repo's venv. Linux: systemd timer units are written to <data_dir>/systemd for `systemctl link`.
Cron expressions in config.claude.schedule are restricted to "M H * * *" (daily), "M * * * *"
(hourly) and "M H * * D" (weekly) forms, which both schedulers can express.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT, Config

TASKS = {"hourly": "hourly", "algo_check": "algo_check"}


def _parse(cron: str) -> dict:
    m, h, dom, mon, dow = cron.split()
    if h == "*" and dow == "*":
        return {"kind": "hourly", "minute": int(m)}
    if dow == "*":
        return {"kind": "daily", "minute": int(m), "hour": int(h)}
    return {"kind": "weekly", "minute": int(m), "hour": int(h), "dow": int(dow)}


def rigctl_cmd() -> str:
    exe = shutil.which("rigctl") or str(Path(sys.executable).with_name("rigctl"))
    return exe


def plan(cfg: Config) -> list[dict]:
    out = []
    sched = cfg.get("claude.schedule", {}) or {}
    for task, key in TASKS.items():
        cron = sched.get(key)
        if not cron:
            continue
        if task == "algo_check" and cfg.get("selector.schedule") == "weekly" and _parse(cron)["kind"] == "daily":
            p = _parse(cron)
            p.update(kind="weekly", dow=1)
        else:
            p = _parse(cron)
        out.append({"task": task, "cron": cron, **p, "cmd": f'"{rigctl_cmd()}" claude-run {task} --trigger schedule --schedule "{cron}"'})
    return out


def windows_commands(cfg: Config) -> list[list[str]]:
    cmds = []
    for p in plan(cfg):
        name = f"rig-overseer {p['task']}"
        base = ["schtasks", "/Create", "/F", "/TN", name, "/TR", p["cmd"], "/RU", "SYSTEM", "/RL", "HIGHEST"]
        if p["kind"] == "hourly":
            base += ["/SC", "HOURLY", "/MO", "1", "/ST", f"00:{p['minute']:02d}"]
        elif p["kind"] == "daily":
            base += ["/SC", "DAILY", "/ST", f"{p['hour']:02d}:{p['minute']:02d}"]
        else:
            days = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
            base += ["/SC", "WEEKLY", "/D", days[p["dow"] % 7], "/ST", f"{p['hour']:02d}:{p['minute']:02d}"]
        cmds.append(base)
    return cmds


def systemd_units(cfg: Config) -> dict[str, str]:
    units = {}
    for p in plan(cfg):
        name = f"rig-overseer-{p['task'].replace('_', '-')}"
        if p["kind"] == "hourly":
            cal = f"*-*-* *:{p['minute']:02d}:00"
        elif p["kind"] == "daily":
            cal = f"*-*-* {p['hour']:02d}:{p['minute']:02d}:00"
        else:
            cal = f"{['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][p['dow'] % 7]} *-*-* {p['hour']:02d}:{p['minute']:02d}:00"
        units[f"{name}.service"] = (f"[Unit]\nDescription=rig-overseer {p['task']} Claude run\nAfter=network-online.target\n\n"
                                    f"[Service]\nType=oneshot\nWorkingDirectory={REPO_ROOT}\nEnvironmentFile=-{cfg.secrets_file}\n"
                                    f"ExecStart={p['cmd']}\nNice=10\n")
        units[f"{name}.timer"] = (f"[Unit]\nDescription=rig-overseer {p['task']} timer\n\n[Timer]\nOnCalendar={cal}\nPersistent=true\n"
                                  f"RandomizedDelaySec=60\n\n[Install]\nWantedBy=timers.target\n")
    return units


def install(cfg: Config, apply: bool) -> list[str]:
    log: list[str] = []
    if sys.platform == "win32":
        for cmd in windows_commands(cfg):
            log.append(" ".join(cmd))
            if apply:
                r = subprocess.run(cmd, capture_output=True, text=True)
                log.append((r.stdout or r.stderr).strip())
    else:
        d = cfg.data_dir / "systemd"
        d.mkdir(parents=True, exist_ok=True)
        for name, body in systemd_units(cfg).items():
            (d / name).write_text(body, encoding="utf-8")
            log.append(f"wrote {d / name}")
        log.append(f"enable with: sudo systemctl link {d}/*.timer {d}/*.service && sudo systemctl enable --now {d}/*.timer")
    return log
