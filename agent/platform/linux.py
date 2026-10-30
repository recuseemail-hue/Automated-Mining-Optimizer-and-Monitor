"""Linux platform (Ubuntu target). CPU temp via hwmon (k10temp/coretemp); CPU power via RAPL when present."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .base import Platform

HWMON = Path("/sys/class/hwmon")
RAPL = Path("/sys/class/powercap/intel-rapl:0/energy_uj")


class LinuxPlatform(Platform):
    name = "linux"

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self._rapl_prev: tuple[float, int] | None = None

    def cpu_temp_c(self) -> float | None:
        if not HWMON.is_dir():
            return None
        for hw in HWMON.iterdir():
            try:
                name = (hw / "name").read_text().strip()
            except OSError:
                continue
            if name in ("k10temp", "coretemp", "zenpower"):
                for f in sorted(hw.glob("temp*_label")):
                    if f.read_text().strip() in ("Tctl", "Tdie", "Package id 0"):
                        return int((hw / f.name.replace("_label", "_input")).read_text()) / 1000
                inp = hw / "temp1_input"
                if inp.exists():
                    return int(inp.read_text()) / 1000
        return None

    def cpu_power_w(self) -> float | None:
        if not RAPL.exists():   # AMD desktop: no RAPL by default -> NULL (documented)
            return None
        try:
            uj = int(RAPL.read_text())
        except OSError:
            return None
        now = time.time()
        prev, self._rapl_prev = self._rapl_prev, (now, uj)
        if prev is None or now - prev[0] <= 0:
            return None
        return max(0.0, (uj - prev[1]) / 1e6 / (now - prev[0]))

    def sensor_notes(self) -> dict[str, str]:
        notes = {
            "fan_rpm": "NVML does not expose fan RPM; only duty %.",
            "memj_c": "No NVML sensor on GeForce. Optional: gddr6 PCI-BAR reader (root) as a later source.",
            "wall_w": "No sensor. Requires a smart plug/meter with a local API (sensors.wall_power).",
        }
        if not RAPL.exists():
            notes["cpu_w"] = "No RAPL on this CPU (AMD desktop); CPU power is NULL."
        return notes

    def is_admin(self) -> bool:
        return os.geteuid() == 0

    def shutdown(self, reason: str, delay_s: int = 30) -> None:
        subprocess.run(["shutdown", "-h", f"+{max(1, delay_s // 60)}", f"rig-overseer: {reason}"], check=False)

    def reboot(self, reason: str, delay_s: int = 30) -> None:
        subprocess.run(["shutdown", "-r", f"+{max(1, delay_s // 60)}", f"rig-overseer: {reason}"], check=False)
