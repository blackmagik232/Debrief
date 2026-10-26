@echo off
cd /d "%~dp0"
title Debrief - check connection
if not exist .venv\Scripts\python.exe (echo Run Setup.bat first. & pause & exit /b 1)
.venv\Scripts\python.exe -m debrief check
if errorlevel 1 echo Something went wrong - see the message above.
echo.
pause
