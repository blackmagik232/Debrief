@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (echo Run Setup.bat first. & pause & exit /b 1)
.venv\Scripts\python.exe -m debrief library
if errorlevel 1 pause
