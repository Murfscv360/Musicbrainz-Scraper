# Run ONCE as Administrator to make PicardWatch fully self-healing.
# Registers a scheduled task that relaunches the supervisor every 10 minutes and at logon
# (a no-op if one is already running, thanks to the single-instance lock). Combined with
# the Startup shortcut + "never sleep" power plan, the watcher recovers from any stop.
#
#   Right-click this file -> "Run with PowerShell" (accept the admin prompt), or from an
#   elevated PowerShell:   powershell -ExecutionPolicy Bypass -File .\install-keepalive.ps1

$proj = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pyw  = Join-Path $proj ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = Join-Path $proj ".venv\Scripts\python.exe" }

$act = New-ScheduledTaskAction -Execute $pyw -Argument ('"{0}"' -f (Join-Path $proj 'keepalive.pyw')) -WorkingDirectory $proj
$t1  = New-ScheduledTaskTrigger -AtLogOn
$t2  = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2))
$repClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskRepetitionPattern
$rep = New-CimInstance -CimClass $repClass -ClientOnly
$rep.Interval = "PT10M"          # every 10 minutes, indefinitely
$t2.Repetition = $rep
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

# Runs as the current user, only when logged on (so the Y: mapped drive is available).
Register-ScheduledTask -TaskName "PicardWatch-Keepalive" -Action $act -Trigger $t1,$t2 -Settings $set `
  -RunLevel Limited -Description "Relaunch PicardWatch supervisor if it stopped (self-heal)." -Force

Write-Host "PicardWatch-Keepalive task registered (every 10 min + at logon)." -ForegroundColor Green
