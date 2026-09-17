@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ Live Notify - Self Check

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

%PY% live_notify.py check

echo.
echo  Press any key to close.
pause >nul
