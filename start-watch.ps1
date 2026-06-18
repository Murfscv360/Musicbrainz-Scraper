#requires -Version 5.1
# Start the PicardWatch watcher daemon. Ctrl+C to stop.
$root = $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'run.py') --watch
