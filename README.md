# rig-overseer

Unattended GPU mining rig overseer: 5-minute telemetry to SQLite, a 15-second local watchdog,
bounded per-GPU tuning, coin selection with hysteresis, a phone-friendly dashboard, and Claude as
the decision layer for scheduled reviews and incidents. Hub/worker: one rig hosts the dashboard and
DB; every other rig (and later the laptops) runs the agent only and forwards to the hub.

Status: **M1 (telemetry + DB)** done. M2–M7 pending. Observe-only by default.

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
```

## Tests
```bash
pytest -q
```
Tests use the simulated GPU and miner backends (`gpu.backend: sim`), including fault injection.

## Docs
- `docs/sensors.md` — what each OS cannot expose and the workaround.
- `docs/system-changes.md` — every OS/software change with undo steps.
- `CLAUDE.md` — operating context and safety rules for Claude sessions.
