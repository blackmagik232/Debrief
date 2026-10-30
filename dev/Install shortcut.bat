@echo off
cd /d "%~dp0.."
title Debrief - add desktop shortcut
if not exist dev\Debrief.vbs (echo dev\Debrief.vbs is missing. & pause & exit /b 1)
set "DEBRIEF_DIR=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:DEBRIEF_DIR.TrimEnd('\'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'Debrief.lnk')); $s.TargetPath=[IO.Path]::Combine($env:SystemRoot,'System32\wscript.exe'); $s.Arguments='\"'+$d+'\dev\Debrief.vbs\"'; $s.WorkingDirectory=$d; $s.IconLocation=$d+'\debrief\desktop\debrief.ico,0'; $s.Description='Review your iRacing races from Garage 61'; $s.Save()"
if errorlevel 1 (echo Could not create the shortcut. & pause & exit /b 1)
echo.
echo  Added "Debrief" to your desktop. Double-click it to open Debrief.
echo.
pause
