@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ===========================================================================
rem  Opens the graphical interface. This is the normal way to use the program.
rem  It first makes sure NapCat is running, then launches the GUI with
rem  pythonw.exe so that no console window appears.
rem
rem  NOTE: never put an unescaped closing paren inside an if(...) block in a
rem  .bat file - it terminates the block early and cmd aborts with
rem  "was unexpected at this time." even when the condition is false.
rem
rem  Keep this file PURE ASCII - see _find-python.bat for the reason.
rem ===========================================================================

call "%~dp0_find-python.bat"
if not defined PYW (
    echo.
    echo  [ERROR] No windowed Python found. Expected pythonw.exe.
    echo  Install Python 3.8+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Preparing NapCat...
call "%~dp0_ensure-napcat.bat"
if errorlevel 1 (
    echo.
    echo  Warning: NapCat is not running. The interface will still open,
    echo  but it cannot send anything until NapCat is up.
    echo.
    pause
)

start "" %PYW% "%~dp0gui.py"
exit /b 0
