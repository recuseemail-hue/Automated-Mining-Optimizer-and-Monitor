# rig-overseer — context for Claude sessions

## What this is
Unattended overseer for Ashton's home GPU mining rigs (Phoenix, AZ; power is free; summer ambient
is hot). Mines Pearl (PRL) on Kryptex with PeakMiner today; RTX 3080 FTW3 now, 4–6+ mixed RTX 30/40/50
later, plus two mining laptops as workers later. Claude is the decision layer for scheduled reviews
and incidents; deterministic code does everything time-critical.

## Machine authority
The rig PCs are dedicated, single-purpose mining computers and are **Claude's to administer**:
install/update software, create services and scheduled jobs, change OS/driver/power/network/firewall
settings, manage files anywhere, as the tool requires. One host (`host.role: hub`) also hosts the
SQLite DB of record, API, dashboard, scheduled Claude runs and notifications; the others are
`worker` and forward telemetry to it. No cloud component.

Guardrails that always apply:
- Never break remote access: Chrome Remote Desktop, AnyDesk, Tailscale, the dashboard route.
- Record every system change in `docs/system-changes.md` with an undo; restore point before OS/driver changes.
- Security exclusions stay narrow (miner folder only). Never disable firewall or antivirus globally.
- Official releases only, hash-verified. No "optimized" miner builds.

## Safety rules (non-negotiable)
- **Funds read-only.** Never request, store or log seed phrases or private keys. No withdrawals,
  trades, transfers. Wallet address and tokens live in env vars / `<data_dir>/secrets.env`
  (`RIG_WALLET_<COIN>`, `NTFY_TOKEN`, `RIG_HUB_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`), never in the repo.
- **Observe-only by default.** `safety.dry_run: true` globally and `gpus[].control_enabled: false`
  per GPU until Ashton flips them.
- **Hardware changes only through the action allowlist** (`agent/actions.py`) with clamps from
  `config.yaml`: power limit, core lock/offset, memory offset, fan, miner start/stop/pause/restart,
  coin/miner/pool switch from the catalog, reboot/shutdown. Never raise voltage above stock, never
  flash BIOS.
- **Bounded loops.** Every trial has a deadline, a max step count and a cooldown. A trial setting is
  never left running past its window; reverts are executed by deterministic code (trial manager,
  watchdog, boot restore) with no LLM or network dependency.
- **One GPU at a time.** Hourly run tunes at most 1 GPU (worst offender). `/optimize` works one GPU at
  a time, 45 min/GPU, 3 h rig cap.
- **LLM calls only for scheduled tasks and incidents**, daily cap in config (start: 10). The 5-minute
  logger and 15-second watchdog never call an LLM. Low free RAM skips scheduled runs, never incidents.
- **Anything beyond the clamps becomes a recommendation to Ashton**, not an action.

## Layout
`agent/` collector, watchdog, actions, trials, optimizer, selector, platform/{windows,linux}, gpu/{nvml,sim},
miner/{peakminer,sim}, pool/kryptex · `api/` FastAPI (hub) · `dashboard/` htmx+uPlot · `tasks/` one
standalone prompt per scheduled Claude task · `docs/` · `config.yaml` (+ `config.local.yaml` per host, gitignored).

## Working here
- Python 3.11+, deps in `pyproject.toml`. Tests: `pytest` (uses the `sim` GPU/miner backends).
- `rigctl` is the CLI and the only tool Claude's scheduled runs are allowed to use for hardware.
- Known unverified items are marked UNVERIFIED in code/docs: PeakMiner `/summary` JSON shape, Kryptex
  API endpoints, Windows fan control via NVML, memory-junction temp source. Verify on the rig, don't assume.
- Scheduled Claude runs are local headless `claude -p` jobs started by the OS scheduler on the hub;
  every prompt in `tasks/` must be standalone (fresh session, no memory).
- Keep status updates short and factual.
