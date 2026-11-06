# Setup checklist

Work top to bottom. Each box is one thing to install, create, connect, or verify. Items marked
**verify** are places where the code made an assumption that must be confirmed on real hardware;
paste the command output into a Claude session on that machine and it will fix the mapping.

Legend: ☐ every host · ☐H hub only · ☐W worker only · ☐P phone

---

## 1. Accounts and services (do once, from any computer)

- ☐ **GitHub** — repo is `recuseemail-hue/Automated-Mining-Optimizer-and-Monitor`. Nothing else to do.
- ☐ **Tailscale** — create an account (tailscale.com). One account for the phone and every PC.
- ☐ **ntfy** — no account needed. Pick a long random topic name, e.g. `rig-7f3k9q2mzx4v`. Write it down; it is the only thing protecting your alerts.
- ☐ **healthchecks.io** (optional) — create a check "rig-overseer hub", period 10 min, grace 5 min. Copy the ping URL.
- ☐ **hashrate.no** — create an account, generate an API key (free plan is enough).
- ☐ **Claude** — Pro or Max subscription on the account you'll use on the hub.
- ☐ **Wallets** — official wallet app for each coin you may mine: Pearl and Quantus (the approved fallback). PeakMiner cannot mine Tari or Conflux, so those need a second miner before they are useful. Save each **receive address**. Seed phrases stay offline and are never typed into the rig or into Claude.
- ☐ **Kryptex** — your Pearl wallet address is your pool login; nothing to create.

## 2. Phone

- ☐P Install **Tailscale**, sign in to the same account.
- ☐P Install **ntfy**, subscribe to your topic.
- ☐P (optional) Install **AnyDesk** / **Chrome Remote Desktop** client if not already.

## 3. Every mining machine (laptop now, rig later)

Install, in this order:
- ☐ **NVIDIA driver** — https://www.nvidia.com/en-us/drivers/ (GeForce RTX 3080, Windows 64-bit; no GeForce Experience/NVIDIA App needed).
- ☐ **Python 3.12** — `winget install Python.Python.3.12` (tick "add to PATH" if using the installer).
- ☐ **Git for Windows** — `winget install Git.Git`.
- ☐ **Tailscale** — install, sign in; note this machine's IP: `tailscale ip -4`.
- ☐ **PeakMiner** — https://github.com/peakminer/peakminer/releases → `peakminer-<version>-windows-x86_64.zip` (v2.20.5 at the time of writing); compare the published SHA256 with `Get-FileHash <file>`. Unzip to `C:\miners\peakminer\`. Add that folder as a Defender exclusion (folder only, nothing global). Do **not** set up `start.bat` to run at login: the agent starts the miner itself (`miner.managed`).
- ☐ **rig-overseer** — elevated PowerShell:
  ```powershell
  git clone https://github.com/recuseemail-hue/Automated-Mining-Optimizer-and-Monitor.git C:\rig-overseer
  cd C:\rig-overseer
  .\install.ps1
  ```
- ☐ Edit `C:\rig-overseer\config.local.yaml`:
  - `host.role`: `hub` on the one machine that will host the dashboard (the laptop for now), `worker` on every other.
  - `miner.exe`: `C:\miners\peakminer\peakminer.exe`
  - workers only: `hub.url: http://<hub tailscale ip>:8080`
- ☐ Secrets (`rigctl secret set NAME` prompts for the value and stores it in Windows Credential Manager):
  - ☐ `RIG_WALLET_PRL` — Pearl receive address
  - ☐ `NTFY_TOPIC` — your topic
  - ☐ `RIG_HUB_TOKEN` — any long random string; **identical on every machine**
  - ☐ `RIG_WALLET_QTC` — Quantus receive address (the approved fallback; no address = no switch)
  - ☐ `HEALTHCHECKS_URL` (optional)
- ☐ Power settings: never sleep, never hibernate, lid close = do nothing, "restart automatically after power failure" in BIOS if available.
- ☐ Windows Update: set active hours so it doesn't reboot mid-day; the agent auto-starts after any reboot regardless.
- ☐ `rigctl config --check` → must print `config OK`.
- ☐ `rigctl sensors` → read the list; anything under "not exposed on this host" is expected (see `docs/sensors.md`).
- ☐ `rigctl gpus` → copy each GPU's `GPU-xxxx` uuid into `config.local.yaml`:
  ```yaml
  gpus:
    - {uuid: GPU-xxxxxxxx-..., name: "3080 FTW3", control_enabled: false}
  ```
- ☐ `rigctl act miner-start --reason "first start"` (live even in observe mode) → the miner starts with the command from `config.yaml` (coin, Kryptex pool, wallet.worker, API key, temp pause at 85 °C). Watch `%ProgramData%\rig-overseer\logs\peakminer.log` for accepted shares.
- ☐ **verify** `rigctl miner dump` → paste the JSON to Claude only if `rigctl status` shows hashrate `-` (the parser follows the official API doc; a mismatch is a doc drift).
- ☐ `rigctl collect` then `rigctl status` → one row with power, temperature, hashrate.
- ☐ `rigctl service install` → agent auto-starts (and the web server on the hub). Check with `rigctl service status`.
- ☐ Reboot once and confirm `rigctl status` keeps updating every 5 min without touching anything.

## 4. Hub only (dashboard, Claude runs)

- ☐H **LibreHardwareMonitor** — https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases (`LibreHardwareMonitor-net472.zip`), run as admin, Options → Remote Web Server → Run, port 8085; Options → Run on Windows startup + Start minimized. This is the only source of CPU temperature and GDDR6X memory-junction temperature on Windows.
- ☐H `rigctl secret set DASHBOARD_PASSWORD`
- ☐H `rigctl secret set HASHRATE_NO_KEY`
- ☐H **Claude Code CLI** — `winget install Anthropic.ClaudeCode`, then in a terminal `claude setup-token`, sign in, copy the token → `rigctl secret set CLAUDE_CODE_OAUTH_TOKEN`.
- ☐H In `config.local.yaml`: `hub: {bind: <this machine's tailscale ip>}` (or keep `127.0.0.1` and run `tailscale serve --bg https+insecure://127.0.0.1:8080` for HTTPS).
- ☐H `rigctl service install` again (adds the web service), then on the phone open `http://<hub tailscale ip>:8080` and log in.
- ☐H `rigctl schedule install` → hourly check and daily coin check in Task Scheduler. `rigctl schedule show` lists them.
- ☐H **verify** `rigctl pool probe` → paste JSON to Claude (balance / pool hashrate fields).
- ☐H **verify** `rigctl coins probe` → paste JSON to Claude (hashrate.no fields). Then `rigctl coins table` should show live $/day.
- ☐H **verify** `rigctl claude-run hourly --dry` prints the command; then `rigctl claude-run hourly` once by hand and check the Claude page of the dashboard shows the run.
- ☐H healthchecks.io: the check should turn green within 10 min of the agent starting.

## 5. Workers only (second rig, laptops)

- ☐W `host.role: worker`, `hub.url` set, same `RIG_HUB_TOKEN` as the hub.
- ☐W `rigctl collect` then on the hub dashboard the new host appears under Hosts within a minute (the forwarder flushes every 60 s).
- ☐W In `config.local.yaml` set `claude: {incident_runs: false}` on machines without the Claude CLI (laptops); they notify only.

## 6. Turning control on (per GPU, after ≥ 24 h of clean observe-only data)

- ☐ In `config.local.yaml` set that GPU to `control_enabled: true`.
- ☐ **verify** `rigctl sensors --probe-write` → every `write …: ok`. Fan refused on Windows GeForce is expected; note what else is refused.
- ☐ `safety: {dry_run: false, emergency_override_dry_run: true}`; restart the agent (`rigctl service uninstall` then `install`, or reboot).
- ☐ `rigctl trial start --gpu 0 --key pl_w --value 230 --hold 600 --reason "first trial"` → after 10 min the GPU page shows it kept or reverted.
- ☐ `rigctl optimize --gpu 0` once (≤ 45 min) → before/after table; the active profile is saved and re-applied at every boot.
- ☐ Leave it. The hourly task only touches a GPU when a trigger fires; the daily task only switches coins with ≥ 15 % gain on two checks.

## 7. Before adding GPUs to the rig

- ☐ PSU budget: `limits.psu_budget_w` is 800 W for the 1000 W unit. Three 3080s at 240 W plus the system is the ceiling; a fourth card needs a second/bigger PSU.
- ☐ Each new card: `rigctl gpus` → add uuid → observe a day → `--probe-write` → enable control.
- ☐ Mixed models: clamps are matched by model name (`clamps.models`); add an entry for each new model before enabling control on it.

## Quick reference: secrets by machine
| Secret | Hub | Worker |
|---|---|---|
| RIG_WALLET_PRL (+ XTM, CFX …) | ✓ | ✓ |
| NTFY_TOPIC (+ NTFY_TOKEN) | ✓ | ✓ |
| RIG_HUB_TOKEN (same value everywhere) | ✓ | ✓ |
| HEALTHCHECKS_URL | ✓ | optional |
| DASHBOARD_PASSWORD | ✓ | – |
| HASHRATE_NO_KEY | ✓ | – |
| CLAUDE_CODE_OAUTH_TOKEN | ✓ | – |
