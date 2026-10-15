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

:: (ออปชันเสริม) ล็อกอิน MT5 อัตโนมัติหากไม่ได้เปิดโปรแกรมทิ้งไว้
:: set MT5_LOGIN=12345678
:: set MT5_PASSWORD=YourPassword
:: set MT5_SERVER=YourBroker-Server

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
echo   VPS Network IP Addresses (สำหรับใส่ในมือถือ):
ipconfig | findstr /i "IPv4"
echo ========================================================
echo Starting FastAPI server on port %PORT%...
echo API Key: %BRIDGE_API_KEY%
echo.
echo [!] Make sure MetaTrader 5 Terminal is OPEN and logged in!
echo [!] If not using Tailscale, make sure port %PORT% is allowed in Windows Firewall!
echo ========================================================
echo.

python vps_mt5_server.py
pause
