#requires -Version 5.1
<#
  PicardWatch installer / launcher.

  Double-click install.bat, OR run from PowerShell:
      .\install.ps1                       # interactive: prompts for paths + AcoustID key, dry-runs
      .\install.ps1 -Launch watch         # ...and start the watcher at the end
      .\install.ps1 -Unattended -Launch none `
                    -InputPath D:\Music\Input -LibraryPath D:\Music\Library `
                    -AcoustidKey XXXX -Autostart        # fully scripted, no prompts

  What it automates: venv, Python deps, the fpcalc (Chromaprint) binary into .\bin,
  Picard detection (optional winget install), writing your answers into config.yaml,
  an optional logon autostart task, and the first run. It cannot create your AcoustID
  account/key for you — it just opens the page and accepts a pasted key.
#>
[CmdletBinding()]
param(
    [string]$InputPath,
    [string]$LibraryPath,
    [string]$AcoustidKey,
    [string]$PicardExe,
    [switch]$Autostart,
    [switch]$InstallPicard,
    [ValidateSet('none', 'dryrun', 'watch')]
    [string]$Launch = 'dryrun',
    [switch]$Unattended
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "[x] $msg" -ForegroundColor Red; exit 1 }
function YesNo($q)  { return ((Read-Host "$q [y/N]") -match '^(y|yes)$') }

# 1) Python -----------------------------------------------------------------
Say "Checking Python..."
$py = $null
foreach ($cand in @('python', 'py')) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $v = & $cand -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]'3.10') { $py = $cand; break }
    }
}
if (-not $py) { Die "Python 3.10+ not found. Install from https://www.python.org/downloads/ then re-run." }
Say "Using $py ($(& $py -c 'import sys;print(sys.version.split()[0])'))"

# 2) Virtual environment ----------------------------------------------------
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Say "Creating virtual environment (.venv)..."
    & $py -m venv (Join-Path $root '.venv')
}
if (-not (Test-Path $venvPy)) { Die "venv creation failed." }

# 3) Dependencies -----------------------------------------------------------
Say "Installing Python dependencies (this can take a minute)..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Die "pip install failed (see output above)." }

# 4) fpcalc (Chromaprint) into .\bin ----------------------------------------
$binDir = Join-Path $root 'bin'
$fpcalc = Join-Path $binDir 'fpcalc.exe'
if (Test-Path $fpcalc) {
    Say "fpcalc already present (bin\fpcalc.exe)"
}
else {
    Say "Downloading fpcalc (Chromaprint)..."
    New-Item -ItemType Directory -Force $binDir | Out-Null
    $url = $null
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/acoustid/chromaprint/releases/latest' `
            -Headers @{ 'User-Agent' = 'PicardWatch-Installer' }
        $asset = $rel.assets | Where-Object { $_.name -match 'windows-x86_64\.zip$' } | Select-Object -First 1
        if ($asset) { $url = $asset.browser_download_url }
    }
    catch { Warn "GitHub API lookup failed: $($_.Exception.Message)" }
    if (-not $url) {
        $url = 'https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-fpcalc-1.6.0-windows-x86_64.zip'
        Warn "Using fallback fpcalc URL."
    }
    $zip = Join-Path $env:TEMP 'pw_fpcalc.zip'
    $tmp = Join-Path $env:TEMP ('pw_fpcalc_' + [guid]::NewGuid().ToString('N'))
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem -Path $tmp -Recurse -Filter 'fpcalc.exe' | Select-Object -First 1
    if (-not $exe) { Die "fpcalc.exe not found inside the downloaded archive." }
    Copy-Item $exe.FullName $fpcalc -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Say "Installed fpcalc -> bin\fpcalc.exe"
}
& $fpcalc -version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "fpcalc did not run cleanly - your antivirus may have quarantined it." }

# 5) Locate Picard ----------------------------------------------------------
Say "Locating MusicBrainz Picard..."
$cands = @(
    $PicardExe,
    "$env:ProgramFiles\MusicBrainz Picard\picard.exe",
    "${env:ProgramFiles(x86)}\MusicBrainz Picard\picard.exe",
    "$env:LOCALAPPDATA\Programs\MusicBrainz Picard\picard.exe"
) | Where-Object { $_ }
$picard = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $picard) {
    $doInstall = $InstallPicard -or (-not $Unattended -and (YesNo "Picard not found. Install it now via winget?"))
    if ($doInstall) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Say "Installing Picard via winget..."
            winget install --id MusicBrainz.Picard -e --accept-package-agreements --accept-source-agreements
            $picard = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
        }
        else { Warn "winget unavailable. Install Picard from https://picard.musicbrainz.org" }
    }
}
if ($picard) { Say "Picard: $picard" } else { Warn "Picard not found - set 'picard.exe' in config.yaml before importing." }

# 6) Write answers into config.yaml (preserves comments) ---------------------
$configPath = Join-Path $root 'config.yaml'
if (-not (Test-Path $configPath)) {
    Copy-Item (Join-Path $root 'config.example.yaml') $configPath
    Say "Created config.yaml from config.example.yaml"
}
function Set-Yaml($content, $key, $value) {
    if (-not $value) { return $content }
    $v = $value -replace '\\', '/'   # forward slashes are YAML-safe and pathlib-friendly
    return [regex]::Replace($content, "(?m)^(\s*$key\s*:\s*`")[^`"]*(`")", "`${1}$v`${2}")
}
if (-not $Unattended) {
    if (-not $InputPath)   { $InputPath   = Read-Host "Input folder to watch   [blank = keep current]" }
    if (-not $LibraryPath) { $LibraryPath = Read-Host "Plex library folder      [blank = keep current]" }
    if (-not $AcoustidKey) {
        Write-Host "A free AcoustID API key enables fingerprint matching." -ForegroundColor DarkCyan
        if (YesNo "Open the AcoustID key signup page now?") { Start-Process 'https://acoustid.org/new-application' }
        $AcoustidKey = Read-Host "Paste your AcoustID API key [blank = skip for now]"
    }
}
$cfg = Get-Content $configPath -Raw
$cfg = Set-Yaml $cfg 'input'   $InputPath
$cfg = Set-Yaml $cfg 'library' $LibraryPath
$cfg = Set-Yaml $cfg 'api_key' $AcoustidKey
$cfg = Set-Yaml $cfg 'exe'     $picard
Set-Content -Path $configPath -Value $cfg -Encoding UTF8
Say "Updated config.yaml"
if ($LibraryPath) { New-Item -ItemType Directory -Force ($LibraryPath) | Out-Null }

# 7) Optional: autostart at logon -------------------------------------------
$doAuto = $Autostart -or (-not $Unattended -and (YesNo "Auto-start the watcher every time you log in?"))
if ($doAuto) {
    try {
        $pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
        $action  = New-ScheduledTaskAction  -Execute $pyw -Argument 'run.py --watch' -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        Register-ScheduledTask -TaskName 'PicardWatch' -Action $action -Trigger $trigger -Settings $set `
            -Description 'PicardWatch music importer' -Force | Out-Null
        Say "Registered logon task 'PicardWatch'. (Remove with: Unregister-ScheduledTask PicardWatch)"
    }
    catch { Warn "Could not register scheduled task: $($_.Exception.Message)" }
}

# 8) Launch -----------------------------------------------------------------
Write-Host ""
switch ($Launch) {
    'dryrun' {
        Say "Dry-run (judges everything, writes review_report.html, MOVES NOTHING)..."
        & $venvPy (Join-Path $root 'run.py') --once --dry-run -v
        Say "Done. Review decisions above / in review_report.html, then:  .\start-watch.ps1"
    }
    'watch' {
        Say "Starting the watcher (Ctrl+C to stop)..."
        & $venvPy (Join-Path $root 'run.py') --watch
    }
    'none' {
        Say "Setup complete. Dry-run with:  .\.venv\Scripts\python.exe run.py --once --dry-run -v"
    }
}
