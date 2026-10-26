@echo off
title Setup .env configuration
cd /d "%~dp0"
echo ========================================================
echo   Setting up .env file for Windows Server
echo ========================================================
echo.

if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env
        echo [OK] Created .env from .env.example successfully!
    ) else (
        echo MAXPLUS_API_KEY=ccsk-your_key_here> .env
        echo BRIDGE_API_KEY=llm-trade-secret-key-2026>> .env
        echo VPS_URL=http://127.0.0.1:8000>> .env
        echo [OK] Created new .env file!
    )
) else (
    echo [INFO] .env already exists.
)

echo.
echo Opening .env in Notepad...
echo (Please paste your MAXPLUS_API_KEY and save with Ctrl+S)
echo ========================================================
notepad .env
