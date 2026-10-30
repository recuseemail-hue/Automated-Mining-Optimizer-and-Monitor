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


CRED_PREFIX = "rig-overseer/"


def _cred_read(name: str) -> str | None:
    """Windows Credential Manager generic credential 'rig-overseer/<NAME>' (stored with `rigctl secret set`)."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]
    adv = ctypes.windll.advapi32
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(CRED_PREFIX + name, 1, 0, ctypes.byref(pcred)):
        return None
    try:
        c = pcred.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        adv.CredFree(pcred)


def set_secret_store(name: str, value: str) -> str:
    """Store a secret: Credential Manager on Windows, else the secrets file (mode 600). Returns where."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        blob = value.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        cred = CREDENTIAL(0, 1, CRED_PREFIX + name, "rig-overseer secret", wintypes.FILETIME(), len(blob),
                          ctypes.cast(buf, ctypes.c_void_p), 2, 0, None, None, "rig-overseer")
        if not ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(f"CredWriteW failed ({ctypes.GetLastError()})")
        return "Windows Credential Manager"
    if _file_path is None:
        raise RuntimeError("secrets file path not configured")
    lines = []
    if _file_path.is_file():
        lines = [ln for ln in _file_path.read_text(encoding="utf-8").splitlines() if not ln.startswith(name + "=")]
    lines.append(f"{name}={value}")
    _file_path.parent.mkdir(parents=True, exist_ok=True)
    _file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(_file_path, 0o600)
    except OSError:
        pass
    global _file_cache
    _file_cache = None
    return str(_file_path)


def get_secret(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name) or _load_file().get(name) or _cred_read(name) or default
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
