@echo off
setlocal
cd /d "%~dp0"
title Debrief setup
echo.
echo  Debrief setup
echo  =============
echo.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo  Python isn't installed yet.
  echo  Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo  or run this in a terminal:  winget install Python.Python.3.12
  echo  Then run Setup.bat again.
  echo.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo  Found a Python that doesn't work or is older than 3.10 ^(it may be the Microsoft Store shortcut^).
  echo  Install Python 3.12 from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo  or run:  winget install Python.Python.3.12
  echo.
  pause
  exit /b 1
)
echo  Using: %PY%
%PY% -m venv .venv || (echo  Could not create the Python environment. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || (echo  Could not install packages. Check your internet connection. & pause & exit /b 1)

if not exist settings.toml (
  copy /y settings.example.toml settings.toml >nul
  echo.
  echo  Created settings.toml. Opening it now:
  echo  paste your Garage 61 token between the quotes after  token =  and save.
  start "" notepad settings.toml
)

echo.
echo  Setup done. Next: double-click "Check connection.bat", then "Review last race.bat".
echo.
pause
