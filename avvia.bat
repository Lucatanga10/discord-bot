@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Discord Bot

set "PY310=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"

echo === AVVIO BOT ===
echo Cartella: %cd%
echo.

if exist "%PY310%" (
    echo Uso Python 3.10: %PY310%
    echo.
    "%PY310%" bot.py
) else (
    echo Python 3.10 non trovato, uso python di sistema
    echo.
    python bot.py
)

echo.
echo === BOT USCITO ===
pause
