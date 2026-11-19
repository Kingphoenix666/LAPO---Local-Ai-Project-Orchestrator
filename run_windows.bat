@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"
echo [LAPO] Initializing Windows launcher...

:: 1. Detect Python 3.10+
set "PY_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if !errorlevel!==0 set "PY_CMD=py -3"
)

if "%PY_CMD%"=="" (
    where python >nul 2>&1
    if %errorlevel%==0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if !errorlevel!==0 set "PY_CMD=python"
    )
)

if "%PY_CMD%"=="" (
    echo [ERROR] Python 3.10 or later is required but was not found in PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/ and check "Add Python to PATH".
    pause
    exit /b 1
)

:: 2. Create local virtual environment if missing
if not exist ".venv\Scripts\python.exe" (
    echo [LAPO] Creating virtual environment in .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"

:: 3. Install / verify dependencies
echo [LAPO] Verifying dependencies...
"%VENV_PY%" -c "import requests, jsonschema" >nul 2>&1
if errorlevel 1 (
    echo [LAPO] Installing requirements from requirements.txt ...
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check internet/proxy settings.
        pause
        exit /b 1
    )
)

:: 4. Final import check
"%VENV_PY%" -c "import lapo, requests, jsonschema" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Package verification failed. Cannot import LAPO modules.
    pause
    exit /b 1
)

:: 5. Launch GUI (no args) or CLI (if args passed)
echo [LAPO] Starting Local AI Project Orchestrator...
"%VENV_PY%" lapo_app.py %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo [LAPO] Exited with code %EXIT_CODE%.
)
exit /b %EXIT_CODE%