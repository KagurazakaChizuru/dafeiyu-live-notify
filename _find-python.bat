@echo off
rem ===========================================================================
rem  Called by the other .bat files via "call". Sets:
rem     PY   - a usable console Python command line
rem     PYW  - the matching windowed interpreter (no console window), for the GUI
rem
rem  Why this dance is needed: the "python.exe" that ships on PATH on Windows
rem  is often the Microsoft Store placeholder stub. Running it fails silently
rem  with exit code 9009. So we probe the official "py" launcher first, then
rem  plain "python", then a few well-known install locations.
rem
rem  IMPORTANT: keep this file PURE ASCII. cmd.exe reads .bat files using the
rem  OEM codepage (GBK on Chinese Windows). UTF-8 Chinese text in a .bat file
rem  desyncs cmd's byte-offset parser, and it ends up trying to execute
rem  fragments of comment lines as commands. Keep Chinese out of .bat files;
rem  all Chinese user-facing text lives inside the Python files instead.
rem
rem  Note: every successful probe jumps to :derive so PYW is always computed.
rem  An early "exit /b 0" would skip the PYW derivation.
rem ===========================================================================

set "PY="

rem --- 1) Official py launcher: most reliable, unaffected by the store stub ---
py -3 --version >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    goto :derive
)

rem --- 2) Try plain python ---
python --version >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :derive
)

rem --- 3) Fall back to common per-user and machine-wide install paths ---
for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python314"
    "%LOCALAPPDATA%\Programs\Python\Python313"
    "%LOCALAPPDATA%\Programs\Python\Python312"
    "%LOCALAPPDATA%\Programs\Python\Python311"
    "%LOCALAPPDATA%\Programs\Python\Python310"
    "%LOCALAPPDATA%\Programs\Python\Python39"
    "%ProgramFiles%\Python314"
    "%ProgramFiles%\Python313"
    "%ProgramFiles%\Python312"
    "%ProgramFiles%\Python311"
    "C:\Python313"
    "C:\Python312"
    "C:\Python311"
    "C:\Python310"
) do (
    if not defined PY if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)

:derive
rem --- Derive the windowed interpreter so the GUI runs without a console ---
rem     "py -3" / "python"  ->  "pyw -3" / "pythonw"
rem     "...\python.exe"    ->  "...\pythonw.exe"
set "PYW="
if /i "%PY%"=="py -3"  set "PYW=pyw -3"
if /i "%PY%"=="python" set "PYW=pythonw"
if not defined PYW if defined PY set "PYW=%PY:python.exe=pythonw.exe%"

exit /b 0
