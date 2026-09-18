@echo off
chcp 65001 >nul

rem ===========================================================================
rem  NapCat launcher for BACKGROUND use - no console window.
rem
rem  Same as launcher-second.bat (it points NapCat at our private QQ copy in
rem  ..\qq-napcat so your own QQ keeps running), but WITHOUT the trailing
rem  "pause". That matters because the GUI runs this with CREATE_NO_WINDOW and
rem  pipes stdout/stderr into its own log view: a "pause" would either hang or
rem  swallow the exit code.
rem
rem  launcher-second.bat is still kept for manual / troubleshooting use, where
rem  you DO want a visible window and a pause.
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
rem That folder holds only the ~8 MB of QQ's top-level files; its versions\
rem directory is a junction into the QQ you already installed, which is what
rem keeps the whole thing small. Repair the junction first in case QQ updated.
if exist "%~dp0..\_fix-qq-link.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\_fix-qq-link.ps1"

for %%A in ("%~dp0..\qq-napcat-private\QQ.exe") do set "QQPath=%%~fA"

if not exist "%QQPath%" (
    echo [ERROR] Private QQ copy not found at:
    echo         %QQPath%
    exit /b 1
)

rem --- optional quick-login account passed as the first argument ---
rem IMPORTANT: pass the BARE QQ number. NapCatWinBootMain.exe converts a bare
rem number into "-q <number>" itself; passing "-q <number>" explicitly does
rem NOT work - the flag gets dropped and NapCat falls back to QR login.
set "ACCOUNT=%~1"

set NAPCAT_MAIN_PATH=%NAPCAT_MAIN_PATH:\=/%
echo (async () =^> {await import("file:///%NAPCAT_MAIN_PATH%")})() > "%NAPCAT_LOAD_PATH%"

"%NAPCAT_LAUNCHER_PATH%" "%QQPath%" "%NAPCAT_INJECT_PATH%" %ACCOUNT%

exit /b %ERRORLEVEL%
