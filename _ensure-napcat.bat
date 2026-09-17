@echo off
rem ===========================================================================
rem  Shared helper, called by the other .bat files via "call".
rem  Ensures NapCat is running and its OneBot port 3000 is listening.
rem
rem  It prefers launcher-second.bat, which runs NapCat on our own private copy
rem  of QQ (app\qq-napcat). That makes NapCat a SEPARATE QQ instance, so the
rem  QQ the user chats with keeps running normally. Only when that copy is
rem  missing does it fall back to the stock launcher - which DOES occupy the
rem  user's QQ.
rem
rem  Everything is resolved relative to this folder (%~dp0), so the whole
rem  package can be moved anywhere without editing any path.
rem
rem  Returns errorlevel 0 when NapCat is ready, 1 when it could not be started.
rem  Keep this file PURE ASCII - see _find-python.bat for the reason.
rem ===========================================================================

set "NAPCAT_DIR=%~dp0napcat"

if not exist "%NAPCAT_DIR%\launcher-user.bat" (
    echo [ERROR] NapCat not found at %NAPCAT_DIR%
    exit /b 1
)

set "LAUNCHER=launcher-user.bat"
if exist "%NAPCAT_DIR%\launcher-second.bat" set "LAUNCHER=launcher-second.bat"

rem --- Already listening? Nothing to do. ---
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo NapCat is already running.
    exit /b 0
)

echo Starting NapCat using %LAUNCHER% ...
set "ACCOUNT="
if exist "%~dp0_account.txt" set /p ACCOUNT=<"%~dp0_account.txt"

if defined ACCOUNT (
    start "NapCat" /D "%NAPCAT_DIR%" cmd /c %LAUNCHER% %ACCOUNT%
) else (
    start "NapCat" /D "%NAPCAT_DIR%" cmd /c %LAUNCHER%
)

echo Waiting for NapCat to become ready...
powershell -NoProfile -Command "for($i=0;$i -lt 45;$i++){ if(Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue){exit 0}; Start-Sleep -Seconds 2 }; exit 1"
if errorlevel 1 (
    echo [ERROR] NapCat did not become ready within 90 seconds.
    echo Look at the NapCat window - you may need to scan a QR code there.
    exit /b 1
)

echo NapCat is ready.
exit /b 0
