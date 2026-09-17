@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ Live Notify - One Click Start

rem ===========================================================================
rem  One click start:
rem    1. ensures NapCat is up      (see _ensure-napcat.bat)
rem    2. starts the stream monitor
rem
rem  Keep this window open while you stream; closing it stops the monitor.
rem
rem  Everything is resolved relative to this folder (%~dp0), so the whole
rem  package can be moved anywhere without editing any path.
rem
rem  Keep this file PURE ASCII - cmd.exe reads .bat files using the OEM codepage
rem  and UTF-8 Chinese text desyncs its byte-offset parser.
rem ===========================================================================

call "%~dp0_find-python.bat"
if not defined PY (
    echo.
    echo  [ERROR] Python not found. Install Python 3.8+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo  [1/2] Preparing NapCat...
call "%~dp0_ensure-napcat.bat"
if errorlevel 1 (
    echo.
    echo  NapCat could not be started. Aborting.
    echo.
    pause
    exit /b 1
)

echo  [2/2] Starting stream monitor. Close this window to stop monitoring.
echo.
%PY% live_notify.py watch

echo.
echo  Monitor stopped. Press any key to close.
pause >nul
