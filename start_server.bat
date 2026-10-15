@echo off
title MT5 VPS Bridge Server for LLM Trading
echo ========================================================
echo   Starting MT5 VPS Bridge Server for Phone Engine
echo ========================================================
echo.

:: กำหนด API KEY (สามารถเปลี่ยนรหัสผ่านนี้ได้ตามต้องการ)
set BRIDGE_API_KEY=llm-trade-secret-key-2026
set PORT=8000

:: (ออปชันเสริม) บังคับคู่เงินเจาะจง เช่น XAUUSD ให้แมปเป็น XAUUSD247 หรือ XAUUSD.crp
:: set SYMBOL_MAP=XAUUSD:XAUUSD247,EURUSD:EURUSD-ECN

echo Checking Python installation...
python --version
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    pause
    exit /b
)

echo Installing / Checking required libraries...
pip install -r requirements_vps.txt

echo.
echo ========================================================
echo Starting FastAPI server on port %PORT%...
echo API Key: %BRIDGE_API_KEY%
echo Make sure MetaTrader 5 Terminal is OPEN and logged in!
echo ========================================================
echo.

python vps_mt5_server.py
pause
