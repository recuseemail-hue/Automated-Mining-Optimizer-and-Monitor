from __future__ import annotations

import platform
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import psutil


@dataclass
class HostSample:
    ts: float
    cpu_c: float | None = None
    cpu_pct: float | None = None
    cpu_w: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    wall_w: float | None = None
    uptime_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Platform:
    name = "base"

    def __init__(self, cfg=None):
        self.cfg = cfg

    def hostname(self) -> str:
        return socket.gethostname()

    def os_string(self) -> str:
        return f"{platform.system()} {platform.release()}"

    def sample(self) -> HostSample:
        vm = psutil.virtual_memory()
        return HostSample(
            ts=time.time(),
            cpu_pct=psutil.cpu_percent(interval=None),
            cpu_c=self.cpu_temp_c(),
            cpu_w=self.cpu_power_w(),
            ram_used_mb=(vm.total - vm.available) / 2**20,
            ram_total_mb=vm.total / 2**20,
            uptime_s=time.time() - psutil.boot_time(),
        )

    def cpu_temp_c(self) -> float | None:
        return None

    def cpu_power_w(self) -> float | None:
        return None

    def sensor_notes(self) -> dict[str, str]:
        """What this OS cannot expose and why; surfaced by `rigctl sensors` and docs/sensors.md."""
        return {}

    def is_admin(self) -> bool:
        return False

    # --- power actions (M2 wires them through the allowlist; never called directly) -----------
    def shutdown(self, reason: str, delay_s: int = 30) -> None:
        raise NotImplementedError

    def reboot(self, reason: str, delay_s: int = 30) -> None:
        raise NotImplementedError

    def create_restore_point(self, description: str) -> bool:
        return False
