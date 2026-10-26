@echo off
cd /d "%~dp0"
if not exist settings.toml copy /y settings.example.toml settings.toml >nul
start "" notepad settings.toml
