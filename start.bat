@echo off
title SAVI NUTRITION - Inventory Management System
cd /d "%~dp0"
echo ============================================
echo   SAVI NUTRITION - Inventory Management System
echo ============================================
echo.
echo Starting the application...
echo Admin: http://localhost:5000
echo User:  http://localhost:5000/user
echo Close this window to stop the server.
echo.
python run_production.py
pause
