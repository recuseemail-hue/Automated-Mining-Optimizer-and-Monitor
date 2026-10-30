# Runbook — what to do when an alert fires

Alerts arrive on the phone (ntfy) and on the dashboard Incidents page. The watchdog has already
acted before you see most of them; your job is to confirm, and to handle what automation cannot.

| Alert | What already happened | What you do |
|---|---|---|
| `overtemp` (core ≥ 85 °C or memory ≥ 105 °C for 30 s) | Ladder: power limit to floor → GPU paused → miner stopped; Claude diagnosis launched. Auto-recovery after 10 min clear (max 3/day); power limit stays at the floor. | Check ambient/airflow and the fan. If it recurs daily in the afternoon, lower `targets.core_c_max` or the summer power limit. Raise the power limit back deliberately via the hourly tuner or `rigctl act pl`. |
| `hard_temp` (core ≥ 95 / memory ≥ 110) | Miner stopped and **PC shut down** (push + heartbeat `/fail` sent first). Dashboard is offline. | Physically check the card (fan, dust, thermal pads). Power on; the agent auto-starts, restores the known-good profile and stays in observe mode until temps are sane. Review the incident and Claude's note. |
| `fan_failure` (0 RPM / 0 % under load for 60 s) | Same ladder as overtemp. | Check the fan. If fan RPM is not exposed (NVML), confirm via LibreHardwareMonitor. Replace the fan or keep the GPU paused: `rigctl act pause --gpu N`. |
| `gpu_missing` (GPU no longer enumerated) | Miner stopped, incident opened, Claude launched (may recommend a reboot; it is capped at 2/day). | Reseat the riser/power cable. `rigctl act reboot --reason "..."` if Claude did not already. A GPU that drops repeatedly: swap the riser first. |
| `miner_down` (0 H/s or miner not running) | Restart after 10 min; crash loop = fast retries up to 3 per 30 min, then stopped + incident. | Read `%ProgramData%\rig-overseer\logs\peakminer.log`. Common causes: pool DNS/outage (check `rigctl pool`), wrong wallet secret, miner update. Start again with `rigctl act miner-start --reason "..."`. |
| `overpower` (GPU power > PSU/circuit budget) | Power limit of the hungriest GPU trimmed one step per minute until under budget. | If you added GPUs: raise `limits.psu_budget_w` only if the PSU really allows it (1000 W unit → 800 W continuous). Otherwise keep the lower limits. |
| `recovery_cap` | Auto-recovery refused (3 recoveries today); GPU stays protected. | Investigate the recurring cause before resuming: `rigctl act resume --gpu N` / `rigctl act miner-start`. |
| `host_offline` (dashboard) / healthchecks.io "down" | Nothing can act: the rig is off, crashed, or lost Tailscale. | Try AnyDesk/Chrome Remote Desktop. If unreachable, the PC is off: power-cycle. Everything restarts automatically. |
| Claude run `error`/`timeout` (Claude page) | The scheduled run failed; the rig keeps running on its last profile. | Check the run output: usually auth (`claude setup-token` expired → `rigctl secret set CLAUDE_CODE_OAUTH_TOKEN`) or the daily cap. |
| `recommendation:` in a Claude summary | Claude found something outside the clamps (airflow, approving a coin, PSU). | Decide; apply yourself or adjust config/clamps and let the next run act. |

## Routine checks
- Weekly: Overview page → balance trend, alert count, each GPU's efficiency vs its profile. `rigctl footprint`
  should stay well under 1 GB RSS total.
- After adding a GPU: `rigctl gpus` → add its uuid under `gpus:` → `rigctl sensors --probe-write` → enable
  control only after a day of clean telemetry.
- After a miner update: official release only, verify the published hash, record it in `docs/system-changes.md`.
- Pearl fork: if the daily check reports an activation height, decide the fallback coin ahead of time and
  create its wallet (`rigctl secret set RIG_WALLET_<COIN>`); without a wallet secret no switch can happen.

## Turning control on (first time)
1. Run observe-only for 24 h (`safety.dry_run: true`). Read `rigctl status`, `rigctl sensors`.
2. `rigctl sensors --probe-write` with the GPU set `control_enabled: true` to prove writes work.
3. Set `safety.dry_run: false`, keep `emergency_override_dry_run: true`. Restart the agent service.
4. Start a manual trial: `rigctl trial start --gpu 0 --key pl_w --value 230 --hold 600 --reason "first trial"` and
   watch it revert or keep on the GPU page.
5. Then `rigctl optimize --gpu 0` once, and let the hourly task take over.

## Stopping everything
`rigctl service uninstall` removes the auto-start tasks; `rigctl act miner-stop --reason "maintenance"` stops the
miner; the dashboard and DB stay as they are.
