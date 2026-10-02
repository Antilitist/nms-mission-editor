@echo off
setlocal
cd /d "%~dp0"
title NMS Mission Editor

REM Python 3.10 or newer, with Tcl/Tk. https://www.python.org/downloads/
REM Tick "Add python.exe to PATH" in the installer.

set "BOOT="
where py >nul 2>&1
if %errorlevel%==0 set "BOOT=py -3"
if not defined BOOT (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3.10 or newer is required.
    echo Install it from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH" and Tcl/Tk.
    pause
    exit /b 1
  )
  set "BOOT=python"
)

%BOOT% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  echo This PC has an older Python.
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo First run: creating a Python environment in this folder.
  %BOOT% -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo Could not create the Python environment.
    echo Install Python 3.10 or newer, including venv and Tcl/Tk.
    pause
    exit /b 1
  )
  "%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
  if errorlevel 1 (
    echo Could not install the required packages.
    pause
    exit /b 1
  )
)

if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run_gui.pyw" %*
) else (
  "%~dp0.venv\Scripts\python.exe" "%~dp0run_gui.pyw" %*
)
endlocal
