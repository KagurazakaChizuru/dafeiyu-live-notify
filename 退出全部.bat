@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Exit DaFeiYu Live Notifier completely

rem ===========================================================================
rem  Closes everything this package started:
rem     1. the notifier (GUI window, or the console monitor)
rem     2. NapCat
rem     3. ONLY the private QQ instance inside app\qq-napcat
rem
rem  It deliberately does NOT touch the QQ you use for chatting. NapCat runs
rem  on its own QQ copy, so the two are separate instances and yours is left
rem  alone.
rem
rem  Keep this file PURE ASCII - see _find-python.bat for the reason.
rem ===========================================================================

echo.
echo  This will close:
echo     - the notifier window
echo     - NapCat
echo     - NapCat's own private QQ instance
echo.
echo  Your own QQ is NOT touched - you can keep chatting.
echo.
set /p GO=Type Y and press Enter to continue: 
if /i not "%GO%"=="Y" (
    echo.
    echo  Cancelled. Nothing was changed.
    echo.
    pause
    exit /b 0
)

echo.
echo  [1/3] Closing the notifier...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and ($_.CommandLine -like '*gui.py*' -or $_.CommandLine -like '*live_notify.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo  [2/3] Closing NapCat and its private QQ instance...
taskkill /IM NapCatWinBootMain.exe /F >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'QQ.exe' -and $_.ExecutablePath -like '*qq-napcat*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul

echo  [3/3] Verifying...
powershell -NoProfile -Command "$left = @(); if (Get-Process -Name NapCatWinBootMain -ErrorAction SilentlyContinue) { $left += 'NapCat' }; if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'QQ.exe' -and $_.ExecutablePath -like '*qq-napcat*' }) { $left += 'private QQ' }; if (Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and ($_.CommandLine -like '*gui.py*' -or $_.CommandLine -like '*live_notify.py*') }) { $left += 'notifier' }; if ($left.Count -eq 0) { Write-Host '  [OK] Everything this package started is closed.'; Write-Host '       Your own QQ was never touched.' } else { Write-Host ('  [WARN] Still running: ' + ($left -join ', ')); Write-Host '         Try again, or end them from Task Manager.' }"

echo.
echo  Press any key to close.
pause >nul
