@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DaFeiYu Live Notifier

rem ===========================================================================
rem  First run: double-click this file.
rem
rem    1. builds the private QQ copy if it is missing - it reads your own QQ
rem       install and makes an ~8 MB clone, so NapCat runs as a SEPARATE QQ
rem       instance and your normal QQ keeps working
rem    2. launches the app
rem
rem  NapCat and QQ's own files are never shipped in the package; step 1
rem  generates the copy locally from what you already have. That is both the
rem  legal way and the small way (8 MB instead of 1.1 GB).
rem
rem  NOTE: never put an unescaped closing paren inside an if(...) block in a
rem  .bat file - it terminates the block early and cmd aborts with
rem  "was unexpected at this time." even when the condition is false.
rem
rem  NOTE: this file never spells out the app's exe name. It is Chinese, and a
rem  .bat with non-ASCII bytes desyncs cmd's byte-offset parser. The wildcard
rem  below finds the only .exe in the folder instead.
rem
rem  Keep this file PURE ASCII - see _find-python.bat for the reason.
rem ===========================================================================

if exist "app\qq-napcat-private" goto launch

echo.
echo  First run - building the private QQ copy.
echo  This reads your installed QQ and takes a few seconds.
echo  QQ must already be installed for this to work.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "app\_setup-qq-copy.ps1"
if errorlevel 1 goto copyfailed
goto launch

:copyfailed
echo.
echo  [WARN] Could not build the private QQ copy.
echo         Install QQ first, then run this file again.
echo.
echo         You can still use the app, but it will occupy your own QQ
echo         instead of running on a separate copy.
echo.
pause

:launch
for %%f in ("%~dp0*.exe") do set "APP=%%f"
if not defined APP goto noexe
start "" "%APP%"
exit /b 0

:noexe
echo.
echo  [ERROR] No .exe found next to this file.
echo         Extract the whole folder before running it.
echo.
pause
exit /b 1
