#requires -Version 5.1
# Print a PicardWatch progress snapshot (albums/tracks moved, remaining, ETA).
# Safe to run anytime, including while the watcher is going.
$root = $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'run.py') --status
