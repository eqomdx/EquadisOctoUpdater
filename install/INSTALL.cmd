@echo off
title Octo Updater - installer
REM ---------------------------------------------------------------------------
REM  Double-click this file.
REM
REM  It installs Python if you do not have it, patches Octo Updater, finds your
REM  OctoWoW folder, backs it up, and builds OctoUpdater.exe.
REM  No administrator rights needed. Safe to run more than once.
REM
REM  Close World of Warcraft before running.
REM
REM  Optional arguments are passed straight through, e.g.
REM      INSTALL.cmd -GameFolder "D:\Octowow"
REM      INSTALL.cmd -GameFolder "E:\Games\OctoWoW" -MigrateFrom "D:\Octowow"
REM ---------------------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
    echo The installer did not finish successfully. See the messages above.
)
pause
exit /b %RC%
