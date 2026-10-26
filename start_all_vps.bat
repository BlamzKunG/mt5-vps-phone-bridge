@echo off
title Start All - MT5 Server + Scalper Engine
echo ========================================================
echo   Starting All Services on VPS (1-Click Launcher)
echo ========================================================
echo.
cd /d "%~dp0"

echo 1. Launching MT5 Bridge Server in separate window...
start "MT5 VPS Bridge Server" cmd /k "start_server.bat"

echo Waiting 3 seconds for server to initialize...
timeout /t 3 /nobreak >nul

echo 2. Launching Scalper Cache Engine in separate window...
start "M5 Scalper Cache Engine" cmd /k "start_cache_scalper_vps.bat"

echo.
echo ========================================================
echo   [OK] Both Server and Engine have been launched!
echo   Please keep both terminal windows OPEN while trading.
echo ========================================================
timeout /t 5
