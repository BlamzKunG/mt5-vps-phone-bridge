@echo off
:: Batch script to allow Port 8000 through Windows Defender Firewall
echo ========================================================
echo   Opening Port 8000 on Windows Firewall...
echo ========================================================
echo.
netsh advfirewall firewall add rule name="MT5 VPS Bridge Port 8000" dir=in action=allow protocol=TCP localport=8000
echo.
if %errorlevel% equ 0 (
    echo [SUCCESS] Port 8000 is now OPEN in Windows Firewall!
) else (
    echo [ERROR] Please right-click this file and select "Run as Administrator"!
)
echo ========================================================
pause
