# rig-overseer

Unattended overseer for home GPU mining rigs: 5-minute telemetry to SQLite, a 15-second local
watchdog with a protective ladder, bounded self-reverting tuning, coin selection with hysteresis, a
phone-friendly dashboard, and Claude as the decision layer for scheduled reviews and incidents.

- **Hub/worker.** One rig (`host.role: hub`, ideally the one with the fewest GPUs) hosts the DB of
  record, API, dashboard, scheduled Claude runs and notifications. Every other rig or laptop runs the
  agent only (`worker`) and forwards telemetry to the hub over Tailscale; a worker keeps protecting its
  own hardware with no hub, no internet and no LLM.
- **Observe-only by default.** `safety.dry_run: true` and `control_enabled: false` per GPU until you
  flip them. Every automated change is one audit row: what, why, who, before/after, ok.
- **No cloud component.** Scheduled Claude runs are local headless `claude -p` jobs started by the
  OS scheduler on the hub, authenticated with a subscription token.

Status: M1–M7 built and unit-tested against the simulator (46 tests). Items that could not be
verified from the build sandbox are listed under *Unverified on real hardware* below.

## Install (every host)
Step-by-step checklist with every account, install, secret and verification: **`docs/setup-checklist.md`**.

Windows (elevated PowerShell), Python 3.11+ and Git installed:
```powershell
git clone <repo> C:\rig-overseer; cd C:\rig-overseer
.\install.ps1
```
Linux (Ubuntu):
```bash
git clone <repo> ~/rig-overseer && cd ~/rig-overseer && ./install.sh
```
The installer creates the venv, installs `rigctl`, writes a starter `config.local.yaml`, creates the DB
and prints `rigctl sensors` (what this host can read and control). Then:

1. Edit `config.local.yaml`: `host.role` (`hub`/`worker`), `miner.exe`, and for workers `hub.url`
   (`http://<hub-tailscale-ip>:8080`). Per-host overrides only; `config.yaml` holds the shared defaults.
2. Secrets (never in the repo): `rigctl secret set <NAME>` stores in Windows Credential Manager, or
   `<data_dir>/secrets.env` (mode 600) on Linux.
   - all hosts: `RIG_WALLET_PRL` (public address only), `NTFY_TOPIC` (long random string), `RIG_HUB_TOKEN`,
     optional `NTFY_TOKEN`, `HEALTHCHECKS_URL`
   - hub: `DASHBOARD_PASSWORD`, `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`)
   - fallback coins: `RIG_WALLET_XTM`, `RIG_WALLET_CFX` … (a coin without a wallet secret can never be switched to)
   - hub: `HASHRATE_NO_KEY` (hashrate.no API key; the daily coin check uses one cached call per GPU model)
3. `rigctl config --check`, `rigctl gpus`, `rigctl collect`, `rigctl status`.
4. `rigctl service install` → auto-start `agent` (and `web` on the hub) at boot, restart on failure.
5. Hub: `rigctl schedule install` → hourly check and daily algorithm check in Task Scheduler / systemd.
6. Hub: expose the dashboard on the tailnet only: set `hub.bind` to `tailscale ip -4`, or keep
   `127.0.0.1` and run `tailscale serve --bg https+insecure://127.0.0.1:8080` for HTTPS. Never port-forward.

Claude Code CLI on the hub: `winget install Anthropic.ClaudeCode` (needs Git for Windows for the Bash
tool) or the Linux installer; then `claude setup-token` and store the token as above.

## Enabling control (per GPU)
1. Run observe-only for a day. Confirm writes work: add the GPU under `gpus:` with
   `control_enabled: true` and run `rigctl sensors --probe-write`.
2. `safety.dry_run: false` and `safety.emergency_override_dry_run: true` in `config.local.yaml`; restart the agent.
3. Try one trial (`rigctl trial start --gpu 0 --key pl_w --value 230 --hold 600 --reason test`), then
   `rigctl optimize --gpu 0` once. The hourly task takes over from there.

## Commands
```text
rigctl config [--check]        effective config, problems, unresolved ${ENV}
rigctl sensors [--probe-write] what this host can read/control; no-op writes to prove control
rigctl gpus | status | collect [--loop] | miner [dump] | pool [probe]
rigctl agent                   agent service: watchdog 15 s, collector 5 min, trial manager, forwarder (worker)
rigctl web                     hub API + dashboard
rigctl act <pl|core-offset|mem-offset|core-lock|fan|miner-*|pause|resume|switch-coin|reboot|shutdown> ...
rigctl trial start|list|abort|tick     bounded self-reverting trials
rigctl profile show|save-current|restore --gpu N
rigctl optimize [--gpu N|all] [--goal max-hashrate|max-efficiency|target-temp] [--window-min 45] [--cap-min 180]
rigctl tune [--pretty]         hourly deterministic pre-check
rigctl coins table|json|probe|fork     profitability (hashrate.no per GPU model) + hysteresis decision; Pearl fork status
rigctl claude-run hourly|algo_check|incident [--dry]   headless Claude run (hub)
rigctl schedule show|plan|install      OS scheduler registration for the Claude runs
rigctl service plan|install|uninstall|status           auto-start services
rigctl secret set NAME | list ; rigctl sys restore-point|log-change ; rigctl footprint
rigctl incidents | actions | sim <uuid> --set core_c=92 (simulator fault injection)
```
`/optimize` is also a Claude Code skill (`.claude/skills/optimize`) that wraps `rigctl optimize`.

## How protection works
1. **Watchdog (15 s, local, no network, no LLM).** Sustained core ≥ 85 °C / memory ≥ 105 °C, fan 0 RPM under
   load, GPU missing, miner down or crash-looping, power over the PSU/circuit budget. Ladder per GPU:
   power limit to floor → pause GPU → stop miner; hard limits (95/110 °C) stop the miner and shut the PC
   down after a push notification and a heartbeat `/fail`. Auto-recovery after 10 min clear, max 3/day.
2. **Push notification** (ntfy) and an **incident** row.
3. **Claude diagnosis** (`tasks/incident.md`) in a detached run: hardware/miner changes only through `rigctl act`
   (clamped), software/system fixes with its admin authority (logged in `docs/system-changes.md`), anything
   beyond the clamps becomes a recommendation.

Trials (hourly tuner and optimizer) are one small step with a deadline; the trial manager judges them by
deterministic scoring and reverts losers; the watchdog reverts anything past its deadline; the service
reverts any open trial at boot and re-applies the saved profile. Optimizer trials past their deadline are
always reverted, never kept.

## Dashboard
Overview (balance coin/USD, coin, total hashrate, power, uptime, alerts), GPUs and per-GPU charts
(1 h–30 d, light/dark, table view), Log (filters, CSV export), Claude (every run: schedule, status,
summary, full output, actions, before/after), Incidents. Phone-first; no external assets (uPlot 1.6.30
vendored, MIT, sha256 `7f29cd14…b4c4f`), so it works during an internet outage.

## Footprint
Measured with the simulator: agent 28 MB RSS, web 51 MB, idle CPU < 1 %. A `claude -p` run adds a few
hundred MB while it runs; scheduled runs are skipped under `claude.min_free_ram_mb` (incidents are not).
`rigctl footprint` prints the live numbers.

## Unverified on real hardware (check on the rig first)
| Item | How to verify | Where to fix |
|---|---|---|
| PeakMiner `/summary` JSON keys and CLI flags | `rigctl miner dump`; compare with `tests/fixtures/peakminer_summary.json` | `agent/miner/peakminer.py` (`parse_summary`, `command`) |
| Kryptex stats API endpoint and fields | `rigctl pool probe` | `pool.stats_url`, `pool.fields`, then `pool.verified: true` |
| hashrate.no API (endpoint paths, auth, field names) | `rigctl secret set HASHRATE_NO_KEY`, then `rigctl coins probe` | `selector.hashrate_no.*`, then `verified: true` |
| NVML fan / VF-offset writes on Windows GeForce | `rigctl sensors --probe-write` | if refused: fan stays on the driver curve; offsets via the miner's own flags (`miner.extra_args`) |
| GDDR6X memory-junction temperature source | `rigctl gpus` → `memj` column | miner API or LibreHardwareMonitor (`sensors.lhm_url`) |
| Claude Code CLI flags on the installed version | `rigctl claude-run hourly --dry` then run it manually once | `agent/claude_runner.py` (`cli_args`, `TOOLS`) |

## Docs
`docs/setup-checklist.md` (hook-up checklist) · `docs/runbook.md` (what to do when an alert fires) · `docs/sensors.md` (what each OS cannot expose) ·
`docs/architecture.md` · `docs/system-changes.md` (every OS/software change with undo) · `CLAUDE.md`.

## Tests
```bash
pytest -q
```
Simulated GPUs and miner with fault injection: overheat, fan failure, GPU drop, crash loop, no-network,
clamps and step limits, trial revert/keep, convergence, trial caps, optimizer windows and caps, crash
mid-trial and hard-kill restore, coin hysteresis and fork, Claude runner gates, API auth and ingest.
