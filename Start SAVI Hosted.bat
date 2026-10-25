@echo off
title SAVI NUTRITION - Hosted website
cd /d "%~dp0"
echo Opening the hosted SAVI Nutrition website...
echo.
echo   Admin: https://admin.savinutritions.online
echo   User:  https://user.savinutritions.online
echo.
python desktop_app.py --hosted
