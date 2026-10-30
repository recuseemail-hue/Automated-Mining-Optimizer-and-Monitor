# Architecture

```
GPUs ─NVML─┐   PeakMiner ─HTTP :4068─┐             Kryptex / profitability APIs (cached in kv)
           ▼                          ▼                          │
[agent service]  watchdog 15 s ─ ladder ─ incidents ─► ntfy push │ ─► healthchecks.io heartbeat
   │             collector 5 min ─────────────────────────────────┘
   │             trial manager (deadline revert) · boot restore · forwarder (worker → hub /api/ingest)
   ▼
SQLite (WAL) ◄── read ── [web service] FastAPI + vanilla JS + vendored uPlot ── Tailscale ──► phone / PC
   ▲
   │ rigctl (clamped action API, audit rows)
Task Scheduler / systemd ─► rigctl claude-run hourly | algo_check ─► claude -p (tasks/*.md + context JSON)
watchdog incident ────────► rigctl claude-run incident (detached) ─┘
```

## Modules (`agent/`)
| Module | Role | LLM? |
|---|---|---|
| `collector` | 5-min samples per GPU + host; registers GPUs; worker outbox | no |
| `watchdog` | 15 s poll, rules, ladder, recovery, 15 s ring, trial deadlines, kv state | no |
| `actions` | the allowlist: clamps, step limits, rate limits, dry-run, audit rows | no |
| `trials` | one-step trials with deadline; judge/keep/revert; boot restore; convergence | no |
| `scoring` | window stats and `compare()` (stability first, then goal score) | no |
| `optimizer` | `/optimize`: baseline, strategies A/B/C, time budget, winner | no |
| `tuner` | hourly pre-check: triggers, exclusions, worst offender, proposal | no |
| `selector` | coin table, fees, hysteresis, data age, Pearl fork state | no |
| `claude_runner` | headless `claude -p` wrapper: gates, tools, prompt, logging | **yes** (scheduled) |
| `incidents` | detached Claude diagnosis per incident | **yes** (incident) |
| `notify`, `heartbeat` | ntfy push with offline queue; healthchecks.io ping | no |
| `services`, `scheduler`, `sysadmin` | auto-start, OS scheduler, restore points, change log | no |
| `gpu/{nvml,sim}`, `miner/{peakminer,sim}`, `platform/{windows,linux}`, `pool/kryptex` | backends | no |

## Data (`agent/db.py`)
`hosts`, `gpus`, `gpu_samples` (5m/15s), `host_samples`, `profiles` (active/known_good/converged),
`trials`, `actions`, `coin_checks`, `claude_runs`, `incidents`, `kv`, `outbox`. All hardware rows carry
`host_id`/`gpu_id`; new GPUs or rigs add rows, not columns.

## Safety invariants
1. Reverts never depend on an LLM or the network: trial manager (deadline), watchdog (every 15 s),
   boot restore (service start).
2. Every control path goes through `Actions`: clamp → step limit → rate limit → dry-run/flag → apply → re-read → audit row.
3. One GPU per hourly run; one GPU at a time in the optimizer; hard windows and caps on everything.
4. Protection runs even when the hub, the internet or Claude are unavailable.
5. Funds are read-only; secrets are outside the repo and scrubbed from logs.
