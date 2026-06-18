#requires -Version 5.1
<#
  PicardWatch installer / launcher.

  Double-click install.bat, OR run from PowerShell:
      .\install.ps1                  # set up, enable logon autostart, start the scanner (background)
      .\install.ps1 -Launch none     # set up only; don't start the scanner now
      .\install.ps1 -NoAutostart     # don't add the logon autostart shortcut

  It is non-interactive when config.yaml is already filled in: it provisions the venv +
  deps + fpcalc, enables a logon autostart shortcut, and launches the watcher in the
  background. It only prompts for an input/library folder or AcoustID key if still unset.
#>
[CmdletBinding()]
param(
    [string]$InputPath,
    [string]$LibraryPath,
    [string]$AcoustidKey,
    [string]$PicardExe,
    [switch]$NoAutostart,
    [switch]$InstallPicard,
    [ValidateSet('none', 'dryrun', 'watch')]
    [string]$Launch = 'watch',
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

# 5) Locate Picard (optional; native tagging is the default engine) ----------
$cands = @(
    $PicardExe,
    "$env:ProgramFiles\MusicBrainz Picard\picard.exe",
    "${env:ProgramFiles(x86)}\MusicBrainz Picard\picard.exe",
    "$env:LOCALAPPDATA\Programs\MusicBrainz Picard\picard.exe"
) | Where-Object { $_ }
$picard = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $picard -and $InstallPicard -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Say "Installing Picard via winget..."
    winget install --id MusicBrainz.Picard -e --accept-package-agreements --accept-source-agreements
    $picard = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if ($picard) { Say "Picard: $picard" } else { Say "Picard not found (optional; native tagging is the default engine)." }

# 6) Configure config.yaml (only prompt for genuinely-missing values) --------
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
function Get-Yaml($content, $key) {
    $m = [regex]::Match($content, "(?m)^\s*$key\s*:\s*`"([^`"]*)`"")
    if ($m.Success) { return $m.Groups[1].Value } else { return "" }
}
$cfg = Get-Content $configPath -Raw
if (-not $Unattended) {
    if (-not $InputPath   -and ((Get-Yaml $cfg 'input')   -in @('', 'D:/Music/Input')))   { $InputPath   = Read-Host "Input folder to watch" }
    if (-not $LibraryPath -and ((Get-Yaml $cfg 'library') -in @('', 'D:/Music/Library'))) { $LibraryPath = Read-Host "Plex library folder" }
    if (-not $AcoustidKey -and ((Get-Yaml $cfg 'api_key') -in @('', 'CHANGE_ME'))) {
        Write-Host "A free AcoustID API key enables fingerprint matching." -ForegroundColor DarkCyan
        if (YesNo "Open the AcoustID key signup page now?") { Start-Process 'https://acoustid.org/new-application' }
        $AcoustidKey = Read-Host "Paste your AcoustID API key"
    }
}
$cfg = Set-Yaml $cfg 'input'   $InputPath
$cfg = Set-Yaml $cfg 'library' $LibraryPath
$cfg = Set-Yaml $cfg 'api_key' $AcoustidKey
$cfg = Set-Yaml $cfg 'exe'     $picard
Set-Content -Path $configPath -Value $cfg -Encoding UTF8
Say "Updated config.yaml"
if ($LibraryPath) { New-Item -ItemType Directory -Force ($LibraryPath) | Out-Null }

# 7) Autostart at logon (on by default; no admin needed) ---------------------
if (-not $NoAutostart) {
    # Startup-folder shortcut: runs at logon in the user session (mapped drives available),
    # needs no Administrator rights, unlike Task Scheduler.
    try {
        $pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path $pyw)) { $pyw = Join-Path $root '.venv\Scripts\python.exe' }
        $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PicardWatch.lnk'
        $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
        $sc.TargetPath = $pyw
        $sc.Arguments = 'run.py --watch'
        $sc.WorkingDirectory = $root
        $sc.WindowStyle = 7   # minimised / hidden (pythonw has no console anyway)
        $sc.Description = 'PicardWatch music importer'
        $sc.Save()
        Say "Autostart enabled (runs at logon): $lnk"
    }
    catch { Warn "Could not create autostart shortcut: $($_.Exception.Message)" }
}

# 8) Launch -----------------------------------------------------------------
Write-Host ""
switch ($Launch) {
    'watch' {
        $pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path $pyw)) { $pyw = $venvPy }
        Start-Process -FilePath $pyw -ArgumentList 'run.py', '--watch' -WorkingDirectory $root -WindowStyle Hidden
        Say "Scanner started in the background. Check progress with:  .\status.ps1"
    }
    'dryrun' {
        Say "Dry-run (judges everything, writes review_report.html, MOVES NOTHING)..."
        & $venvPy (Join-Path $root 'run.py') --once --dry-run -v
    }
    'none' {
        Say "Setup complete. Start the scanner with:  .\start-watch.ps1"
    }
}
