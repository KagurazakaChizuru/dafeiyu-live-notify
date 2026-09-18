@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DaFeiYu Live Notifier - Watching (close this window to stop)

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

echo  Starting stream monitor... it will notify your groups automatically.
echo.

%PY% live_notify.py watch

echo.
echo  Monitor stopped. Press any key to close.
pause >nul
