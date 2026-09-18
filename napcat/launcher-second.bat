@echo off
chcp 65001 >nul

rem ===========================================================================
rem  NapCat launcher - uses OUR OWN PRIVATE COPY of QQ.
rem
rem  Difference from the stock launcher-user.bat: that one looks up the
rem  system-wide QQ installation in the registry and launches it. QQ is
rem  normally single-instance, so NapCat would occupy the same QQ you use for
rem  chatting.
rem
rem  This launcher instead points NapCat at app\qq-napcat-private, a SHARED
rem  copy of the QQ installation: only the ~8 MB of top-level files are real,
rem  and versions\ is a junction into the QQ you already have installed.
rem  Windows resolves the exe path to qq-napcat-private\QQ.exe, which it treats
rem  as a different application, so it becomes a second independent instance -
rem  your own QQ keeps running - while costing almost no extra disk.
rem
rem  Keep this file PURE ASCII - cmd.exe reads .bat files using the OEM codepage
rem  and UTF-8 Chinese text desyncs its byte-offset parser.
rem ===========================================================================

set NAPCAT_PATCH_PACKAGE=%cd%\qqnt.json
set NAPCAT_LOAD_PATH=%cd%\loadNapCat.js
set NAPCAT_INJECT_PATH=%cd%\NapCatWinBootHook.dll
set NAPCAT_LAUNCHER_PATH=%cd%\NapCatWinBootMain.exe
set NAPCAT_MAIN_PATH=%cd%\napcat.mjs

rem --- private QQ copy lives one level up, in ..\qq-napcat-private\ ---
rem Repair the version junction first in case QQ updated itself.
if exist "%~dp0..\_fix-qq-link.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\_fix-qq-link.ps1"

for %%A in ("%~dp0..\qq-napcat-private\QQ.exe") do set "QQPath=%%~fA"

if not exist "%QQPath%" (
    echo [ERROR] Private QQ copy not found at:
    echo         %QQPath%
    echo.
    echo Falling back is not automatic - run launcher-user.bat instead,
    echo but note that it WILL occupy your normal QQ.
    pause
    exit /b 1
)

set NAPCAT_MAIN_PATH=%NAPCAT_MAIN_PATH:\=/%
echo (async () =^> {await import("file:///%NAPCAT_MAIN_PATH%")})() > "%NAPCAT_LOAD_PATH%"

"%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" %*

pause
