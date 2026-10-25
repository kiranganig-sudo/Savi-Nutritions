@echo off
title SAVI NUTRITION - Offline (local database)
cd /d "%~dp0"
echo ============================================
echo   SAVI NUTRITION - Offline mode
echo ============================================
echo.
echo This starts the server on THIS computer using the local database.
echo Use it only when the hosted website is unreachable.
echo.
echo Admin: http://localhost:5000
echo User:  http://localhost:5000/user
echo Close this window to stop the server.
echo.
set "SAVI_MODE=local"
python desktop_app.py --local
pause
