#!/usr/bin/env bash
# rig-overseer bootstrap for Linux (Ubuntu). Run from the repo folder; idempotent.
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 (3.11+) required"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[web,dev]"
sudo mkdir -p /var/lib/rig-overseer/logs 2>/dev/null || mkdir -p data/logs
if [ ! -f config.local.yaml ]; then
cat > config.local.yaml <<'EOF'
host: {role: hub}            # hub | worker (workers: set hub.url to the hub's Tailscale address)
miner: {exe: /opt/peakminer/peakminer}
gpus: []                     # add {uuid: ..., control_enabled: true} after `rigctl gpus`
EOF
echo "wrote config.local.yaml - edit host.role, miner.exe, gpus[]"
fi
rigctl config --check
rigctl init-db
rigctl sensors
cat <<EOF

Next:
  rigctl secret set RIG_WALLET_PRL        # and NTFY_TOPIC, RIG_HUB_TOKEN, DASHBOARD_PASSWORD (hub), CLAUDE_CODE_OAUTH_TOKEN (hub)
  rigctl collect && rigctl status
  sudo \$(pwd)/.venv/bin/rigctl service install     # systemd units: agent (+ web on the hub)
  rigctl schedule install                            # systemd timers for the Claude runs (hub)
EOF
