@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ Live Notify - GUI (debug)

rem Same as the normal GUI launcher, but keeps a console window so that any
rem startup error is visible instead of silently disappearing.
rem (Keep this file pure ASCII - see _find-python.bat for why.)

call "%~dp0_find-python.bat"
if not defined PY (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

%PY% "%~dp0gui.py"

echo.
echo GUI exited. Press any key to close.
pause >nul
