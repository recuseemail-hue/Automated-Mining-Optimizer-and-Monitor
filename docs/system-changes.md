# System changes log

Every change this tool (or Claude acting through it) makes to the host OS, drivers, services,
firewall, power settings or installed software is recorded here with how to undo it. Restore
points are created before OS/driver changes.

Format: `YYYY-MM-DD HH:MM host | who (user/claude/rule) | change | undo`

| Date | Host | Who | Change | Undo |
|---|---|---|---|---|
| 2026-11-06 | rig1 | user | Installed PeakMiner 2.20.5 (peakminer-2.20.5-windows-x86_64.zip, SHA256 3F339A85F9E01B58AF67F7EA77E037B3C0C4C75E5DC5E8008D3CDA6E570388E3) to C:\miners\peakminer; Defender folder exclusion added | Remove-MpPreference -ExclusionPath C:\miners\peakminer; delete the folder |
| 2026-11-06 | rig1 | user | LibreHardwareMonitor (release zip) to C:\tools\LibreHardwareMonitor, web server on 8085, run at startup | Options → untick Run On Windows Startup; delete the folder |
| 2026-11-06 | rig1 | user | NVIDIA Studio driver 621.14 (clean install) | Device Manager → Display adapters → Roll Back Driver |
| 2026-11-06 | rig1 | user | PowerShell execution policy CurrentUser=RemoteSigned (needed for install.ps1) | Set-ExecutionPolicy -Scope CurrentUser Undefined |
| 2026-11-06 | rig1 | user | Installed Python 3.12, Git, Tailscale (IP 100.97.182.90), Claude Code via winget | winget uninstall each id |
| 2026-11-06 | rig1 | user | Task Scheduler tasks: rig-overseer agent, rig-overseer web (boot, SYSTEM), rig-overseer hourly, rig-overseer algo_check | rigctl service uninstall; schtasks /Delete /TN "rig-overseer hourly" and "rig-overseer algo_check" |
| 2026-11-06 | rig1 | user | PeakMiner managed by the agent; secrets moved to C:\ProgramData\rig-overseer\secrets.env (SYSTEM+Admins ACL) | delete the file; re-enter secrets |
