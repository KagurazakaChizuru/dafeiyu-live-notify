@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ Live Notify - Rehearsal

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

%PY% live_notify.py test

echo.
echo  This was a rehearsal. Nothing was actually sent to any group.
echo  Press any key to close.
pause >nul
