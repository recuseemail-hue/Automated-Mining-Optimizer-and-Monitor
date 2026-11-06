# Sensor coverage by OS

Run `rigctl sensors` on every new host; it prints the live version of this table. Values the OS
cannot expose are stored as NULL, never guessed.

| Metric | Windows (NVML) | Linux (NVML) | Workaround |
|---|---|---|---|
| GPU power, core temp, fan %, clocks, PL, util, VRAM, throttle flags | yes | yes | — |
| GPU memory junction temp (GDDR6X) | **no** on GeForce | **no** on GeForce | PeakMiner's API does not report it (v2.20.5). 1) LibreHardwareMonitor web server (`sensors.lhm_url`, NVAPI "GPU Memory Junction"); 2) Linux: `gddr6` PCI-BAR reader as root (not wired yet). If none works the 105 °C memory rule is inactive and the memory-offset clamp is tightened instead. |
| GPU fan RPM | **no** (duty % only) | **no** | LibreHardwareMonitor exposes RPM via NVAPI. The "0 RPM under load" watchdog rule uses `fan_rpm` when present, else falls back to `fan_pct == 0` plus rising temperature. |
| Core/memory VF offsets (read) | driver ≥ 520 + nvidia-ml-py ≥ 12 | same | `rigctl sensors` shows `core_offset_read` / `mem_offset_read`. |
| Fan control (write) | **works** on rig1 (RTX 3080 FTW3, driver 621.x: fixed 80 % set via NVML, 2026-11-06); fallback: PeakMiner `/control/oc` fan duty (`gpu.control_backend: auto`) | works as root on most cards | `rigctl sensors --probe-write` reports the NVML answer and whether the miner OC path is available. If both refuse, fan stays on the driver curve and the ladder goes power-limit → pause → stop. |
| Power limit / locked clocks (write) | Administrator required | root required | Services run elevated. |
| CPU temperature | **no** native API | hwmon (k10temp) | Windows: LibreHardwareMonitor web server. |
| CPU package power | **no** | Intel RAPL only; AMD desktop = NULL | LibreHardwareMonitor on Windows. |
| RAM, CPU load, uptime | yes (psutil) | yes | — |
| Wall power | no | no | Smart plug/meter with local API (`sensors.wall_power`), not configured yet. |
| Miner hashrate / shares | PeakMiner HTTP API (`/summary`, H/s, per-GPU accepted/invalid shares) | same | Matches the official API.md for v2.20.5; `rigctl miner dump` confirms on the rig. |
| Pool hashrate / balance | Kryptex HTTP API | same | Endpoint and field paths **unverified**; `rigctl pool probe` prints raw JSON; set `pool.verified: true` after mapping. Cached in `kv` so outages don't blank the dashboard. |

## Memory junction temperature: why it matters here
Phoenix summer ambient plus GDDR6X on an open frame: the memory can hit 100 °C+ while the core
reads 70 °C. Until a working source is confirmed on the rig, treat the memory-offset upper clamp
(`clamps.*.mem_offset_mhz[1]`) as the primary protection and keep the fan floor at 60 % when
`memj_c` is NULL.
