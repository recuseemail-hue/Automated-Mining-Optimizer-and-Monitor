"""Windows platform.

CPU temperature/power: Windows exposes neither to user code without a kernel driver. We read them
from LibreHardwareMonitor's HTTP server (sensors.lhm_url) when it is installed and running; the
same endpoint is one of the memory-junction temperature sources. Without it they are logged NULL.
"""
from __future__ import annotations

import ctypes
import json
import logging
import re
import subprocess
import time
import urllib.request

from .base import Platform

log = logging.getLogger(__name__)


class LhmReader:
    """Tiny cache over LibreHardwareMonitor /data.json (tree of {Text, Children, Value})."""

    def __init__(self, url: str | None, ttl_s: float = 10.0, timeout_s: float = 2.0):
        self.url, self.ttl, self.timeout = url, ttl_s, timeout_s
        self._tree = None
        self._at = 0.0
        self.available = False

    def tree(self):
        if not self.url:
            return None
        if self._tree is None or time.time() - self._at > self.ttl:
            try:
                with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                    self._tree = json.loads(r.read().decode("utf-8", "replace"))
                self.available = True
            except Exception:
                self._tree = None
                self.available = False
            self._at = time.time()
        return self._tree

    @staticmethod
    def _walk(node, path):
        yield path, node
        # a node is identified by its text AND its icon (LHM names the CPU node after the chip, e.g.
        # "AMD Ryzen 3 3100", while its ImageURL is images_icon/cpu.png)
        tag = f"{node.get('Text', '')}|{node.get('ImageURL', '')}"
        for ch in node.get("Children", []) or []:
            yield from LhmReader._walk(ch, path + [tag])

    @staticmethod
    def _value(node) -> float | None:
        v = str(node.get("Value", "")).split(" ")[0]
        if "," in v and "." in v:
            v = v.replace(",", "")                      # 2,180.5
        elif re.fullmatch(r"-?\d{1,3}(,\d{3})+", v):
            v = v.replace(",", "")                      # 2,180 (thousands separator)
        else:
            v = v.replace(",", ".")                     # 52,4 (decimal comma locales)
        try:
            return float(v)
        except ValueError:
            return None

    def gpu_fan_rpm(self, ordinal: int = 0) -> float | None:
        """First fan RPM of the n-th GPU device (LHM 'GPU Fan 1' under the GPU node)."""
        t = self.tree()
        if not t:
            return None
        gpus: list[float] = []
        for path, node in self._walk(t, []):
            if not node.get("Children") and node.get("Text", "").lower().startswith("gpu fan") and \
                    any("nvidia" in p.lower() or "amd" in p.lower() or "gpu" in p.lower() for p in path):
                if node.get("Text", "").strip().lower().endswith("1") or not gpus:
                    gpus.append(self._value(node))
        return gpus[ordinal] if ordinal < len(gpus) else None

    def find(self, device_contains: str, sensor_contains: str) -> float | None:
        t = self.tree()
        if not t:
            return None
        for path, node in self._walk(t, []):
            if node.get("Children"):
                continue
            if sensor_contains.lower() in node.get("Text", "").lower() and \
                    any(device_contains.lower() in p.lower() for p in path):
                return self._value(node)
        return None

    def gpu_memory_junction(self, gpu_name_hint: str, ordinal: int = 0) -> float | None:
        """LHM reports 'GPU Memory Junction' for RTX 30/40 via NVAPI. Match the n-th GPU device."""
        t = self.tree()
        if not t:
            return None
        hits = []
        for path, node in self._walk(t, []):
            if not node.get("Children") and "memory junction" in node.get("Text", "").lower():
                hits.append(self._value(node))
        return hits[ordinal] if ordinal < len(hits) else None


class WindowsPlatform(Platform):
    name = "windows"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        url = cfg.get("sensors.lhm_url") if cfg else None
        self.lhm = LhmReader(url)

    def cpu_temp_c(self) -> float | None:
        for label in ("Core (Tctl/Tdie)", "CPU Package", "Core (Tdie)", "CCD1 (Tdie)", "Core Average", "CPU Core"):
            v = self.lhm.find("cpu", label)
            if v is not None:
                return v
        return None

    def cpu_power_w(self) -> float | None:
        if not self.lhm.available:
            return None
        for label in ("Package", "CPU Package", "Core (SMU)"):
            v = self.lhm.find("cpu", label)
            if v is not None:
                return v
        return None

    def sensor_notes(self) -> dict[str, str]:
        notes = {
            "fan_rpm": "NVML does not expose fan RPM; only duty %. RPM requires LibreHardwareMonitor (NVAPI).",
            "memj_c": "NVML has no GDDR6X junction sensor on GeForce. Sources: miner API, LibreHardwareMonitor.",
            "wall_w": "No sensor. Requires a smart plug/meter with a local API (sensors.wall_power).",
        }
        if not self.lhm.available:
            notes["cpu_c/cpu_w"] = "Need LibreHardwareMonitor running with its web server on sensors.lhm_url."
        return notes

    def is_admin(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def shutdown(self, reason: str, delay_s: int = 30) -> None:
        subprocess.run(["shutdown", "/s", "/t", str(delay_s), "/c", f"rig-overseer: {reason}"[:120]], check=False)

    def reboot(self, reason: str, delay_s: int = 30) -> None:
        subprocess.run(["shutdown", "/r", "/t", str(delay_s), "/c", f"rig-overseer: {reason}"[:120]], check=False)

    def create_restore_point(self, description: str) -> bool:
        ps = ("Checkpoint-Computer -Description '{}' -RestorePointType 'MODIFY_SETTINGS'"
              .format(description.replace("'", "")))
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("restore point failed: %s", (r.stderr or r.stdout).strip()[:300])
        return r.returncode == 0
