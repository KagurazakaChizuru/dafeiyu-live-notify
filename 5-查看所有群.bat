@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ Live Notify - Group List

call "%~dp0_find-python.bat"
if not defined PY (
    echo.
    echo  [ERROR] Python not found. Install Python 3.8+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

%PY% live_notify.py groups

echo.
echo  Press any key to close.
pause >nul
