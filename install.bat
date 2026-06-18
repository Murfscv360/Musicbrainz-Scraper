@echo off
REM Double-click me. Runs the PicardWatch installer with a sane execution policy.
REM Pass-through args work too, e.g.:  install.bat -Launch watch
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
