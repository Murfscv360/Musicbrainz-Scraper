#requires -Version 5.1
<#
  Stop PicardWatch cleanly.
    .\stop.ps1            # graceful: finish the current album, then the supervisor + watcher exit
    .\stop.ps1 -Force     # kill immediately (no waiting)
    .\stop.ps1 -Disable   # also remove the logon autostart shortcut (so it won't restart at logon)
#>
[CmdletBinding()]
param([switch]$Force, [switch]$Disable)

$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

function Get-PWProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*run.py*--supervise*' -or $_.CommandLine -like '*run.py*--watch*' }
}

if (-not $Force) {
    Write-Host "Requesting graceful stop (finish current album, then exit)..." -ForegroundColor Cyan
    & $py (Join-Path $root 'run.py') --stop
    $deadline = (Get-Date).AddMinutes(4)
    while ((Get-Date) -lt $deadline -and (Get-PWProcs)) { Start-Sleep -Seconds 5 }
}

$left = Get-PWProcs
if ($left) {
    Write-Host "Force-stopping remaining processes..." -ForegroundColor Yellow
    $left | ForEach-Object { & taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null }
    Start-Sleep -Seconds 2
}

# Clear the stop flag so a future start isn't immediately halted.
Remove-Item (Join-Path $root 'picardwatch.stop') -Force -ErrorAction SilentlyContinue

if ($Disable) {
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PicardWatch.lnk'
    Remove-Item $lnk -Force -ErrorAction SilentlyContinue
    Write-Host "Autostart disabled (removed the logon shortcut)." -ForegroundColor Yellow
}

if (Get-PWProcs) { Write-Host "WARNING: PicardWatch still running." -ForegroundColor Red }
else { Write-Host "PicardWatch stopped." -ForegroundColor Green }
