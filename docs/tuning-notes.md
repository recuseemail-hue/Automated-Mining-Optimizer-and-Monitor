# Tuning notes — rig1, EVGA RTX 3080 FTW3 Ultra 10 GB (non-LHR), pearlhash

Measured 2026-11-06 with SRBMiner 3.8.0 (1-minute averages, ±5 % noise), stock vBIOS 94.02.42.80.16.
Stock power limit 380 W (max 400 W). Starting profile seeded as stock.

| Setting | Hashrate | Power | Core | Temp | Eff (GH/s/W) |
|---|---|---|---|---|---|
| stock 380 W | ~125 TH/s | 377 W | ~1875 MHz | 71–72 °C | 332 |
| 400 W | 124 TH/s | 392 W | unchanged (not power-limited) | | 316 |
| 400 W + mem lock 5001 | 122 TH/s | 346 W | | | 353 |
| core lock 1800 | 119 TH/s | 297 W | | | 401 |
| core lock 1700 | 115 TH/s | 255 W | | | 451 |
| core 1700 + mem 5001 | 112 TH/s (~105 sustained) | 227 W | | | 493 |
| core lock 1400 | 94 TH/s | 173 W | | | 543 |

Takeaways
- The algorithm is **core-bound**: memory 9251 → 5001 MHz costs ~2.5–3 % hashrate and saves ~46 W.
- With free power the owner's goal is **max hashrate within temperature targets** (`optimize.default_goal:
  max-hashrate`). Stock already sits at 72 °C core; summer ambient will push that over the 75 °C target, so
  the hourly tuner's first lever on a hot day is the core lock (−50 MHz steps), then power limit.
- Next lever for hashrate: positive core offset (+75, then +25 steps) watching compute errors / invalid shares.
  Strategy B in `config.yaml` encodes exactly that.
- Memory offset is not worth exploring on this algorithm; memory lock 5001 is the efficiency knob.
- PSU: one card at 380 W is fine; three cards at stock would exceed the 800 W budget — the watchdog's
  overpower rule trims the hungriest card, but plan core locks (~1700 → 255 W each) before adding GPU 3.
- Miners seen for PearlPoW on unMineable: SRBMiner, LPMiner, PeakMiner, ForgeMiner. Only PeakMiner is
  integrated; SRBMiner-MULTI would also cover Tari/Conflux — candidate second backend.

Miner comparison (same card, stock, pearlhash, 2026-11-06): SRBMiner-MULTI via the unMineable app reported
~133 TH/s (567/3 shares) where PeakMiner 2.20.5 reported ~125 TH/s. Re-check over 24 h on the dashboard before
concluding; if it holds, a managed SRBMiner backend is worth building (task: SRBMiner-MULTI backend).

## 2026-11-06 — MSI Afterburner profile (owner-applied, external controller)
Afterburner 4.6.6 (profile slots 1 and 5): power limit 105 % (400 W), core offset **+125 MHz**, memory +0,
fans fixed 80 % (set via NVML; resets on reboot — NVML fan writes confirmed working on this driver).
Result with SRBMiner on pearlhash: **~133.7 TH/s @ ~394 W**, core 2010 MHz, core 64 °C, hot spot 78 °C,
memory junction 70 °C, 0 errors. Sweep: +150 clean for 90 s (not soak-tested), **+175 → nvlddmkm event 153
driver resets**. Fallback if instability appears: +100. Clamp for this card set to +150; strategy B stops at +125.

While Afterburner owns the clocks the GPU carries `external_oc: afterburner`: the hourly tuner and
`/optimize` refuse it; only watchdog emergency actions touch it. Remove the flag (and close Afterburner)
to hand tuning back to the tool.

unMiner runs SRBMiner with `--disable-worker-watchdog`: after a driver reset it stays up at 0 H/s. The
watchdog treats "alive + 0 H/s for 10 min" as a failure and kills it so the app relaunches it
(`miner.kill_stalled_external`).

2026-11-07 after reboot (Afterburner not running, fan on auto curve, PL 400 W persisted): core 71 °C, **hot spot 86 °C**,
memory junction 78 °C at 398 W, 1995 MHz, SRBMiner 132.6 TH/s. Hot spot is the number to manage in summer; Afterburner
must start with Windows and apply the 80 % fan at startup, or the profile is lost on every reboot.
