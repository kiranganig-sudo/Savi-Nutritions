@echo off
title Stop SAVI Nutrition
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'desktop_app\.py|run_production\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo SAVI Nutrition server stopped.
ping -n 3 127.0.0.1 >nul
