"""Dashboard auth: signed session cookie (password login) for the UI, bearer token for /api.

Tailscale (or Cloudflare Access) is the first layer; this is the second. The signing key is
generated once into <data_dir>/dashboard.key. Login attempts are rate-limited per client.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as pysecrets
import time
from pathlib import Path

from agent.secrets import get_secret

COOKIE = "rs"


class Auth:
    def __init__(self, data_dir: Path, *, session_days: int = 30, lockout_s: int = 60, secure: bool = False):
        self.key = self._load_key(data_dir / "dashboard.key")
        self.ttl = session_days * 86400
        self.lockout_s = lockout_s
        self.secure = secure
        self._fails: dict[str, list[float]] = {}

    @staticmethod
    def _load_key(path: Path) -> bytes:
        if path.is_file():
            return path.read_bytes()
        key = pysecrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key

    # --- password ----------------------------------------------------------------------------
    @staticmethod
    def password_configured() -> bool:
        return bool(get_secret("DASHBOARD_PASSWORD"))

    def check_password(self, client: str, password: str) -> bool:
        now = time.time()
        fails = [t for t in self._fails.get(client, []) if now - t < self.lockout_s]
        self._fails[client] = fails
        if len(fails) >= 5:
            return False
        expected = get_secret("DASHBOARD_PASSWORD") or ""
        ok = bool(expected) and hmac.compare_digest(password.encode(), expected.encode())
        if not ok:
            fails.append(now)
        return ok

    # --- session cookie ------------------------------------------------------------------------
    def issue(self) -> str:
        body = f"{int(time.time())}.{pysecrets.token_urlsafe(12)}"
        sig = hmac.new(self.key, body.encode(), hashlib.sha256).digest()
        return body + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def verify(self, token: str | None) -> bool:
        if not token or token.count(".") != 2:
            return False
        ts, nonce, sig = token.split(".")
        body = f"{ts}.{nonce}"
        want = base64.urlsafe_b64encode(hmac.new(self.key, body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(sig, want):
            return False
        try:
            return time.time() - int(ts) < self.ttl
        except ValueError:
            return False

    # --- bearer --------------------------------------------------------------------------------
    @staticmethod
    def check_bearer(header: str | None) -> bool:
        tok = get_secret("RIG_HUB_TOKEN")
        if not tok or not header or not header.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), tok)
