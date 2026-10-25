@echo off
cd /d "%~dp0"
setlocal

rem --- Find git: PATH first, then GitHub Desktop's bundled copy ---------------
set "GITEXE="
where git >nul 2>nul && set "GITEXE=git"
if not defined GITEXE (
  for /d %%D in ("%LOCALAPPDATA%\GitHubDesktop\app-*") do (
    if exist "%%D\resources\app\git\cmd\git.exe" set "GITEXE=%%D\resources\app\git\cmd\git.exe"
  )
)
if not defined GITEXE (
  echo Git was not found on this computer.
  echo Install Git or GitHub Desktop, then try again.
  pause
  exit /b 1
)

echo ================================
echo   SAVE CHANGES TO GITHUB
echo   https://github.com/kiranganig-sudo/Savi-Nutritions
echo ================================
set /p msg=Type what you changed and press Enter: 
if "%msg%"=="" set msg=Manual save

"%GITEXE%" add -A
"%GITEXE%" commit -m "%msg%"
"%GITEXE%" remote get-url origin >nul 2>nul || "%GITEXE%" remote add origin https://github.com/kiranganig-sudo/Savi-Nutritions.git
"%GITEXE%" push origin main
echo.
if errorlevel 1 goto failed
echo DONE - your changes are on GitHub.
echo Upload them to the server (see DEPLOY_NAMECHEAP.md), then restart both apps.
goto end
:failed
echo Something went wrong - show this screen to whoever helps you.
:end
pause
endlocal
