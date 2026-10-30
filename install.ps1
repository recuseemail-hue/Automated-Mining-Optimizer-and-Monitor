# rig-overseer bootstrap for Windows (run in an elevated PowerShell from the repo folder).
# Idempotent: safe to re-run after `git pull`.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.11+ is required (winget install Python.Python.3.12)" }
if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -e ".[web,dev]"

$data = Join-Path $env:ProgramData "rig-overseer"
New-Item -ItemType Directory -Force -Path $data, (Join-Path $data "logs") | Out-Null
if (-not (Test-Path config.local.yaml)) {
@"
host: {role: hub}            # hub | worker (workers: set hub.url to the hub's Tailscale address)
miner: {exe: 'C:\miners\peakminer\peakminer.exe'}
gpus: []                     # add {uuid: ..., control_enabled: true} after `rigctl gpus`
"@ | Set-Content config.local.yaml
  Write-Host "wrote config.local.yaml - edit host.role, miner.exe, gpus[]"
}

$rigctl = Join-Path $root ".venv\Scripts\rigctl.exe"
& $rigctl config --check
& $rigctl init-db
& $rigctl sensors
Write-Host ""
Write-Host "Next:"
Write-Host "  $rigctl secret set RIG_WALLET_PRL       # and NTFY_TOPIC, RIG_HUB_TOKEN, DASHBOARD_PASSWORD (hub), CLAUDE_CODE_OAUTH_TOKEN (hub)"
Write-Host "  $rigctl collect && $rigctl status       # one sample"
Write-Host "  $rigctl service install                  # auto-start agent (+ web on the hub)"
Write-Host "  $rigctl schedule install                 # hourly + algo-check Claude runs (hub)"
