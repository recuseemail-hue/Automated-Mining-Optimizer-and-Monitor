"""OS abstraction: host sensors, power actions, services. Windows first, Linux second."""
from __future__ import annotations

import platform as _platform

from .base import Platform


def get_platform(cfg=None) -> Platform:
    if _platform.system() == "Windows":
        from .windows import WindowsPlatform
        return WindowsPlatform(cfg)
    from .linux import LinuxPlatform
    return LinuxPlatform(cfg)


__all__ = ["Platform", "get_platform"]
