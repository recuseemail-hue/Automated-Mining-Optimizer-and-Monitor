"""Secret lookup and log redaction.

Order: environment variable -> secrets file (KEY=VALUE lines) -> None.
Values are remembered only so the log filter can scrub them. Nothing here writes secrets.
Wallet seed phrases and private keys are never requested or stored anywhere in this tool.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_known_values: set[str] = set()
_file_cache: dict[str, str] | None = None
_file_path: Path | None = None


def configure(secrets_file: Path | None) -> None:
    global _file_cache, _file_path
    _file_path = secrets_file
    _file_cache = None


def _load_file() -> dict[str, str]:
    global _file_cache
    if _file_cache is not None:
        return _file_cache
    out: dict[str, str] = {}
    if _file_path and _file_path.is_file():
        for line in _file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    _file_cache = out
    return out


def get_secret(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name) or _load_file().get(name) or default
    if val:
        _known_values.add(val)
    return val


def wallet_for(coin: str) -> str | None:
    return get_secret(f"RIG_WALLET_{coin.upper()}")


def redact(text: str) -> str:
    for v in _known_values:
        if len(v) >= 6 and v in text:
            text = text.replace(v, v[:3] + "…" + v[-2:])
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = redact(msg)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:
            pass
        return True
