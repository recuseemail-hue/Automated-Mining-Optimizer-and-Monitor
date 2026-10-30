"""Auto-start services for the agent (every host) and the web server (hub).

Windows: Task Scheduler tasks (built in, no third-party service wrapper): run at startup as SYSTEM,
hidden, unlimited duration, restart on failure every minute, below-normal priority. Managed via an
XML definition because the schtasks CLI cannot express restart-on-failure.
Linux: systemd units with Restart=always, Nice=10 and a memory cap.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT, Config
from .scheduler import rigctl_cmd

SERVICES = {"agent": "agent", "web": "web"}


def service_names(cfg: Config) -> list[str]:
    return ["agent"] + (["web"] if cfg.is_hub else [])


def task_xml(name: str, command: str, args: str, working_dir: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>rig-overseer {name} (auto-start, restart on failure)</Description></RegistrationInfo>
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author"><Exec><Command>{command}</Command><Arguments>{args}</Arguments><WorkingDirectory>{working_dir}</WorkingDirectory></Exec></Actions>
</Task>
"""


def systemd_unit(cfg: Config, name: str) -> str:
    mem = "512M" if name == "web" else "256M"
    return (f"[Unit]\nDescription=rig-overseer {name}\nAfter=network-online.target\nWants=network-online.target\n"
            f"StartLimitIntervalSec=0\n\n[Service]\nType=simple\nWorkingDirectory={REPO_ROOT}\n"
            f"EnvironmentFile=-{cfg.secrets_file}\nExecStart={rigctl_cmd()} {SERVICES[name]}\nRestart=always\nRestartSec=10\n"
            f"Nice=10\nMemoryMax={mem}\nKillSignal=SIGTERM\nTimeoutStopSec=30\n\n[Install]\nWantedBy=multi-user.target\n")


def plan(cfg: Config) -> list[dict]:
    out = []
    for name in service_names(cfg):
        out.append({"name": name, "task_name": f"rig-overseer {name}", "command": rigctl_cmd(), "args": SERVICES[name],
                    "workdir": str(REPO_ROOT)})
    return out


def install(cfg: Config, apply: bool) -> list[str]:
    log: list[str] = []
    d = cfg.data_dir / "services"
    d.mkdir(parents=True, exist_ok=True)
    for p in plan(cfg):
        if sys.platform == "win32":
            xml = d / f"{p['name']}.xml"
            xml.write_text(task_xml(p["name"], p["command"], p["args"], p["workdir"]), encoding="utf-16")
            cmd = ["schtasks", "/Create", "/F", "/TN", p["task_name"], "/XML", str(xml)]
            log.append(" ".join(cmd))
            if apply:
                r = subprocess.run(cmd, capture_output=True, text=True)
                log.append((r.stdout or r.stderr).strip())
                r = subprocess.run(["schtasks", "/Run", "/TN", p["task_name"]], capture_output=True, text=True)
                log.append((r.stdout or r.stderr).strip())
        else:
            unit = d / f"rig-overseer-{p['name']}.service"
            unit.write_text(systemd_unit(cfg, p["name"]), encoding="utf-8")
            log.append(f"wrote {unit}")
            if apply:
                for cmd in (["systemctl", "link", str(unit)], ["systemctl", "enable", "--now", unit.name]):
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    log.append(" ".join(cmd) + ": " + (r.stdout or r.stderr).strip())
    if sys.platform == "win32" and apply:
        log.append("record in docs/system-changes.md: Task Scheduler tasks 'rig-overseer agent'/'rig-overseer web' (undo: rigctl service uninstall)")
    return log


def uninstall(cfg: Config) -> list[str]:
    log = []
    for p in plan(cfg):
        if sys.platform == "win32":
            for cmd in (["schtasks", "/End", "/TN", p["task_name"]], ["schtasks", "/Delete", "/F", "/TN", p["task_name"]]):
                r = subprocess.run(cmd, capture_output=True, text=True)
                log.append(" ".join(cmd) + ": " + (r.stdout or r.stderr).strip())
        else:
            unit = f"rig-overseer-{p['name']}.service"
            r = subprocess.run(["systemctl", "disable", "--now", unit], capture_output=True, text=True)
            log.append(f"systemctl disable --now {unit}: " + (r.stdout or r.stderr).strip())
    return log


def status(cfg: Config) -> list[str]:
    out = []
    for p in plan(cfg):
        if sys.platform == "win32":
            r = subprocess.run(["schtasks", "/Query", "/TN", p["task_name"], "/FO", "LIST"], capture_output=True, text=True)
            out.append(f"{p['task_name']}: " + " ".join(line.strip() for line in r.stdout.splitlines() if "Status" in line) or r.stderr.strip())
        else:
            r = subprocess.run(["systemctl", "is-active", f"rig-overseer-{p['name']}.service"], capture_output=True, text=True)
            out.append(f"rig-overseer-{p['name']}: {(r.stdout or r.stderr).strip()}")
    return out


def footprint() -> list[dict]:
    """RSS/CPU of rig-overseer processes (agent, web, claude runs, miner) for the budget check."""
    import psutil
    rows = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent", "create_time"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        if any(k in cmd for k in ("rigctl", "agent.rigctl", "claude -p", "peakminer")) and "footprint" not in cmd:
            rows.append({"pid": p.info["pid"], "rss_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 2**20, 1),
                         "cpu_pct": p.cpu_percent(interval=0.5), "cmd": cmd[:100]})
    return rows


def lower_priority() -> None:
    """Mining never waits on us: below-normal on Windows, nice 10 on Linux."""
    try:
        import psutil
        me = psutil.Process()
        if sys.platform == "win32":
            me.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            me.nice(10)
    except Exception:
        pass
