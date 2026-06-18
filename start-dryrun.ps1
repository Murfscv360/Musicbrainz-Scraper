#requires -Version 5.1
# Safe dry-run: judge every album, write review_report.html, move nothing.
$root = $PSScriptRoot
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'run.py') --once --dry-run -v
