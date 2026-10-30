# rig-overseer — context for Claude sessions

## What this is
Unattended overseer for Ashton's home GPU mining rigs (Phoenix, AZ; power is free; summer ambient is
hot). Mines Pearl (PRL) on Kryptex with PeakMiner today; one RTX 3080 FTW3 now, 4–6+ mixed RTX 30/40/50
later, plus two mining laptops as workers. Claude is the decision layer for scheduled reviews and
incidents; deterministic code does everything time-critical. See `docs/architecture.md`.

## Machine authority
The rig PCs are dedicated, single-purpose mining computers and are **Claude's to administer**:
install/update software, create services and scheduled jobs, change OS/driver/power/network/firewall
settings, manage files anywhere, as the tool requires. One host (`host.role: hub`) also hosts the
SQLite DB of record, API, dashboard, scheduled Claude runs and notifications; the others are
`worker` and forward telemetry to it. No cloud component.

Guardrails that always apply:
- Never break remote access: Chrome Remote Desktop, AnyDesk, Tailscale, the dashboard route.
- Record every system change in `docs/system-changes.md` with an undo (`rigctl sys log-change`);
  create a restore point before OS/driver changes (`rigctl sys restore-point "<why>"`).
- Security exclusions stay narrow (miner folder only). Never disable firewall or antivirus globally.
- Official releases only, hash-verified. No "optimized" miner builds.

## Safety rules (non-negotiable)
- **Funds read-only.** Never request, store or log seed phrases or private keys. No withdrawals,
  trades, transfers. Wallet addresses and tokens live in env vars / Credential Manager /
  `<data_dir>/secrets.env` (`RIG_WALLET_<COIN>`, `NTFY_TOPIC`, `RIG_HUB_TOKEN`, `DASHBOARD_PASSWORD`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `HEALTHCHECKS_URL`), never in the repo or logs.
- **Observe-only by default.** `safety.dry_run: true` globally and `gpus[].control_enabled: false`
  per GPU until Ashton flips them. The watchdog ladder acts in dry-run only with
  `safety.emergency_override_dry_run: true`.
- **Hardware changes only through the action allowlist** (`agent/actions.py`, exposed as `rigctl act`,
  `rigctl trial`, `rigctl optimize`) with clamps and per-change step limits from `config.yaml`:
  power limit, core lock/offset, memory offset, fan, miner start/stop/pause/restart, coin switch
  from the catalog, reboot/shutdown. Never raise voltage above stock, never flash BIOS.
- **Bounded loops.** Every trial has a deadline, a step cap, a daily cap and a cooldown. A trial setting
  is never left running: the trial manager, the watchdog and the boot path revert without any LLM or
  network dependency. Optimizer trials past their deadline are always reverted.
- **One GPU at a time.** Hourly: at most 1 GPU (worst offender), only if a trigger fired. `/optimize`:
  one GPU at a time, 45 min/GPU, 3 h rig cap.
- **LLM calls only for scheduled tasks and incidents** (`claude.daily_cap`, start 10/day; incidents may
  go to 2×). The 5-minute logger and the 15-second watchdog never call an LLM. Low free RAM skips
  scheduled runs, never incident runs. `claude.hourly_gating` skips the call when there is no candidate.
- **Anything beyond the clamps becomes a recommendation to Ashton**, not an action.
- **Coins:** only `coins.<X>.auto_switch: true` may be switched to automatically (Pearl, Tari, Conflux).
  Quantus and others need Ashton's approval. A coin without a wallet secret can never be switched to.

## Layout
`agent/` collector, watchdog, actions, trials, scoring, optimizer, tuner, selector, claude_runner,
incidents, notify, heartbeat, services, scheduler, sysadmin, platform/{windows,linux}, gpu/{nvml,sim},
miner/{peakminer,sim}, pool/kryptex · `api/` FastAPI (hub) · `dashboard/` templates + static (vendored
uPlot) · `tasks/` one standalone prompt per Claude task (hourly, algo_check, incident) · `docs/` ·
`config.yaml` (+ `config.local.yaml` per host, gitignored) · `install.ps1` / `install.sh` ·
`.claude/skills/optimize`.

## Working here
- Python 3.11+, deps in `pyproject.toml` (`pip install -e .[web,dev]`). Tests: `pytest -q` (simulator
  backends; tests never touch real hardware, the network, or power state — see `tests/conftest.py`).
- `rigctl` is the CLI and the only hardware interface scheduled Claude runs may use
  (`agent/claude_runner.py: TOOLS`). Prompts in `tasks/` must stay standalone: each run is a fresh session.
- Scheduled runs are local headless `claude -p` jobs started by Task Scheduler / systemd on the hub
  (cloud scheduled tasks cannot reach the rig unattended). Auth: subscription token from
  `claude setup-token`; an API key is capped per run by `claude.max_budget_usd`.
- Known UNVERIFIED items (marked in code/docs; verify on the rig, never assume): PeakMiner `/summary`
  JSON shape and CLI flags, Kryptex API endpoints, profitability source, Windows fan/offset writes via
  NVML, memory-junction temperature source, exact Claude Code CLI flags on the installed version.
- Scope: v1 is this rig; laptops join as workers (expect OEM-locked power limits/fans). Do not add
  features beyond the brief without asking; if an extra change would materially help, explain it
  briefly and ask first. Ask a specific question instead of guessing when a change could be destructive.
- Keep status updates short and factual.
