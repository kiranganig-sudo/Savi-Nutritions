@echo off
title SAVI NUTRITION - Production
cd /d "%~dp0"
echo ============================================
echo   SAVI NUTRITION - Production
echo ============================================
echo.
echo Admin: http://localhost:5000
echo User:  http://localhost:5000/user
echo Close this window to stop the server.
echo.
python run_production.py
pause
