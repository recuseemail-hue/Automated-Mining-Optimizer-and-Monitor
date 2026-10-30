---
name: optimize
description: Time-boxed per-GPU tuning (A efficiency undervolt, B max-hashrate, C balanced/cool). One GPU at a time, every step self-reverting.
argument-hint: [--gpu N|all] [--goal max-hashrate|max-efficiency|target-temp] [--window-min 45] [--cap-min 180]
allowed-tools: Bash(rigctl optimize *), Bash(rigctl optimize), Bash(rigctl status), Bash(rigctl profile *), Bash(rigctl trial *)
---

Run the optimizer for the requested GPU(s) and report the result. Rules you must follow:

1. Do not change any GPU setting yourself. The only hardware path is `rigctl optimize`; it applies
   clamps, step limits, time windows and automatic reverts. Never call `rigctl act` from this command.
2. Before starting, check `rigctl status` and refuse to run if a GPU shows an active ladder/incident
   or if the miner is not running.
3. Run (it blocks for up to the window per GPU, 3 h rig cap; use a long timeout):

   `rigctl optimize $ARGUMENTS`

4. When it finishes, report the before/after table it printed verbatim, then two or three lines:
   which strategy won per GPU and why, anything that was reverted as unstable, and whether the
   result was applied or the original settings were restored.
5. If the command exited non-zero or was interrupted, say so, run `rigctl trial list` to confirm no
   trial is left open, and `rigctl profile show --gpu <N>` to show the settings now in force.
