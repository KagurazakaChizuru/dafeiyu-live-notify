@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DaFeiYu Live Notifier - Send Now

call "%~dp0_find-python.bat"
if not defined PY (
    echo.
    echo  [ERROR] Python not found.
    echo  Please install Python 3.8+ from https://www.python.org/downloads/
    echo  and be sure to tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

%PY% live_notify.py send

echo.
echo  Press any key to close.
pause >nul
