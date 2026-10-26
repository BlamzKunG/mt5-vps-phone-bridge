@echo off
title M5 Scalper Engine (Localhost VPS)
echo ========================================================
echo   Starting M5 Scalper Engine directly on VPS
echo ========================================================
echo.
cd /d "%~dp0"

echo Checking Python installation...
python --version
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

echo Checking required libraries...
pip install -r requirements_phone.txt

if not exist ".env" (
    echo.
    echo [WARNING] .env file not found!
    echo Please make sure .env exists with your MAXPLUS_API_KEY!
    echo Creating .env from .env.example...
    if exist ".env.example" (
        copy .env.example .env
        echo Created .env - Please open .env and put your real MAXPLUS_API_KEY!
        pause
    )
)

echo.
echo ========================================================
echo   Target: Localhost VPS Bridge (127.0.0.1:8000)
echo   Make sure MetaTrader 5 and start_server.bat are running!
echo ========================================================
echo.

set VPS_URL=http://127.0.0.1:8000
set BRIDGE_API_KEY=llm-trade-secret-key-2026

python phone_scalper_engine.py
pause
