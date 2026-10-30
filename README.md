# rig-overseer

Unattended GPU mining rig overseer: 5-minute telemetry to SQLite, a 15-second local watchdog,
bounded per-GPU tuning, coin selection with hysteresis, a phone-friendly dashboard, and Claude as
the decision layer for scheduled reviews and incidents. Hub/worker: one rig hosts the dashboard and
DB; every other rig (and later the laptops) runs the agent only and forwards to the hub.

Status: M1 telemetry + DB, M2 watchdog + action API, M3 dashboard, M4 /optimize, M5 scheduled Claude tasks, **M6 incident escalation** done. M7 (services, hardening, docs) pending. Observe-only by default.

## Install (per host)
```bash
git clone <repo> rig-overseer && cd rig-overseer
python -m venv .venv && .venv/Scripts/activate   # Windows; Linux: . .venv/bin/activate
pip install -e .[dev]
```
Create `config.local.yaml` next to `config.yaml`:
```yaml
host: {role: hub}            # or worker, with hub.url pointing at the hub's Tailscale address
miner: {exe: 'C:\miners\peakminer\peakminer.exe'}
```
Create `%ProgramData%\rig-overseer\secrets.env` (owner-only ACL) with `RIG_WALLET_PRL=<address>`.
Then:
```bash
rigctl config            # effective settings, unresolved secrets
rigctl sensors           # what this host can read/control (do this first on every rig)
rigctl gpus              # live GPU sample
rigctl collect           # one 5-minute sample row
rigctl collect --loop    # the logger (becomes a service in M7)
rigctl status            # latest stored values
rigctl miner dump        # raw PeakMiner API JSON -> save to tests/fixtures to verify the parser
rigctl pool probe        # raw Kryptex JSON -> fix pool.fields, then pool.verified: true
rigctl agent             # the agent service: watchdog 15 s + collector 5 min (+ forwarder on workers)
rigctl act pl 230 --gpu 0 --reason "..."     # clamped, logged control action (dry-run until enabled)
rigctl trial start --gpu 0 --key mem_offset_mhz --value 100 --hold 900   # self-reverting trial
rigctl profile show --gpu 0 | rigctl incidents | rigctl actions
rigctl web               # hub only: API + dashboard on hub.bind:hub.port (becomes a service in M7)
rigctl optimize --gpu 0 --goal max-efficiency   # time-boxed tuning (45 min/GPU, 3 h rig cap); also /optimize in Claude Code
rigctl tune --pretty     # hourly deterministic pre-check: candidates, triggers, proposed step
rigctl coins table       # profitability table + hysteresis decision; `coins probe` raw source JSON
rigctl claude-run hourly --dry      # print the exact claude command + prompt without calling Claude
rigctl schedule install  # register hourly + algo-check runs with Task Scheduler / write systemd timers
```

## Scheduled Claude runs (hub)
- Local headless `claude -p` jobs started by the OS scheduler; no cloud scheduled tasks. Each run gets a
  standalone prompt from `tasks/<task>.md` plus a context JSON built by code. Tools are restricted to the
  listed `rigctl` commands (`agent/claude_runner.py: TOOLS`); every action is tagged with the run id and
  shown on the Claude page of the dashboard.
- Auth: `claude setup-token` on the hub (Pro/Max subscription) → `CLAUDE_CODE_OAUTH_TOKEN=...` in the secrets
  file. An `ANTHROPIC_API_KEY` works too and is then capped per run by `claude.max_budget_usd`.
- Requires the Claude Code CLI on PATH and, on Windows, Git for Windows (the Bash tool is what the
  permission rules target).
- Gates: `claude.daily_cap` (incidents may go to 2×), `claude.min_free_ram_mb` (scheduled runs only),
  `claude.hourly_gating` (no LLM call when the pre-check finds no candidate; the run is still logged).
- Incidents: the watchdog acts first (local ladder, no network), pushes to the phone, then launches
  `rigctl claude-run incident --incident <id>` detached (one run per incident, min 15 min between launches,
  `claude.incident_runs`). The run's summary is pushed to the phone and linked on the Incidents page.
- Off-box heartbeat: put a healthchecks.io ping URL in secret `HEALTHCHECKS_URL`; the agent pings every 5 min
  and hits `/fail` with the reason right before any watchdog shutdown.
- Optimizer time budget: 10 min baseline, then the remaining window is split evenly across strategies
  A/B/C; with 5-min holds that is about 2 steps per strategy inside 45 min (`optimize.*` to change).

## Dashboard (hub)
- Secrets on the hub: `DASHBOARD_PASSWORD` (UI login), `RIG_HUB_TOKEN` (workers' ingest + API bearer).
- `hub.bind` must be the hub's Tailscale IP (`tailscale ip -4`). Expose HTTPS with
  `tailscale serve --bg https+insecure://127.0.0.1:8080` or bind directly to the tailnet IP; never port-forward.
- Workers: `host.role: worker`, `hub.url: http://<hub-tailscale-ip>:8080`, same `RIG_HUB_TOKEN`.
- Pages: Overview (balance, coin, hashrate, power, uptime, alerts), GPUs + per-GPU charts (1h–30d, table view),
  Log (filters + CSV), Claude (run history with full output and actions), Incidents.
- No external assets: uPlot 1.6.30 is vendored (`dashboard/static/vendor`, MIT, sha256
  `7f29cd14…b4c4f`), so the dashboard works during an internet outage.

## Enabling control (per GPU)
1. `safety.dry_run: false` in `config.local.yaml`.
2. Add the GPU with `control_enabled: true` under `gpus:` (uuid from `rigctl gpus`).
3. Recommended once stable: `safety.emergency_override_dry_run: true` so the watchdog ladder acts
   even while the rest of the tool is in dry-run.
Secrets for alerts: `NTFY_TOPIC` (long random string), optional `NTFY_TOKEN`.

## Tests
```bash
pytest -q
```
Tests use the simulated GPU and miner backends (`gpu.backend: sim`), including fault injection.

## Docs
- `docs/sensors.md` — what each OS cannot expose and the workaround.
- `docs/system-changes.md` — every OS/software change with undo steps.
- `CLAUDE.md` — operating context and safety rules for Claude sessions.
