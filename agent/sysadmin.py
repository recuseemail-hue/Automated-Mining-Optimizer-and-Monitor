"""Helpers for Claude's system-level authority: restore points and the system-changes log.

Every OS/driver/software change goes through log_change() so docs/system-changes.md stays the
single undo list. create_restore_point() is called before major OS or driver changes on Windows.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import REPO_ROOT, Config
from .platform import get_platform

DOC = REPO_ROOT / "docs" / "system-changes.md"


def log_change(cfg: Config, who: str, change: str, undo: str, host: str | None = None) -> str:
    line = f"| {time.strftime('%Y-%m-%d %H:%M')} | {host or cfg.host_name} | {who} | {change.replace('|', '/')} | {undo.replace('|', '/')} |\n"
    text = DOC.read_text(encoding="utf-8") if DOC.is_file() else "# System changes log\n\n| Date | Host | Who | Change | Undo |\n|---|---|---|---|---|\n"
    text = text.replace("| _none yet_ | | | | |\n", "")
    if not text.endswith("\n"):
        text += "\n"
    DOC.write_text(text + line, encoding="utf-8")
    return line.strip()


def restore_point(cfg: Config, description: str) -> bool:
    plat = get_platform(cfg)
    ok = plat.create_restore_point(description)
    log_change(cfg, "claude", f"restore point: {description}", "System Restore -> pick this point" if ok else "n/a (creation failed)")
    return ok
