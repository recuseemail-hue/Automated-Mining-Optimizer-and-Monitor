"""Logging setup: rotating file in <data_dir>/logs plus stderr, with secret redaction."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .secrets import RedactFilter

_configured = False


def setup_logging(data_dir: Path, name: str, level: str = "INFO", max_mb: int = 20, backups: int = 5) -> logging.Logger:
    global _configured
    root = logging.getLogger()
    if not _configured:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            logs / f"{name}.log", maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8"
        )
        sh = logging.StreamHandler(sys.stderr)
        for h in (fh, sh):
            h.setFormatter(fmt)
            h.addFilter(RedactFilter())
            root.addHandler(h)
        _configured = True
    return logging.getLogger(name)
