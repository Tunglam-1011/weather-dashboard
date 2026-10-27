@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Weather Data Board

echo.
echo   Weather Data Board - starting
echo   Dir: %CD%
echo.

set "PYEXE="

rem ---------------------------------------------------------------
rem  Find a working interpreter.
rem  Rule: existence is not enough, it must actually execute once.
rem  The Microsoft Store python.exe is only a stub - it opens the
rem  Store and exits 0, so anything with WindowsApps in the path is
rem  skipped on purpose.
rem  All branching uses labels, never if ( ... ) blocks, because
rem  %errorlevel% inside a block is expanded at parse time.
rem ---------------------------------------------------------------

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto found

for /f "delims=" %%P in ('where python 2^>nul') do call :try_python "%%P"
if defined PYEXE goto found

call :try_python "python"
if defined PYEXE goto found

goto nopython


:try_python
if defined PYEXE goto :eof
echo "%~1" | find /i "WindowsApps" >nul
if not errorlevel 1 goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PYEXE="%~1""
goto :eof


:found
echo   Python: %PYEXE%
%PYEXE% -c "import sys;print('   version:',sys.version.split()[0]);print('   exe    :',sys.executable)"
echo.
echo   Starting server - the browser will open automatically.
echo   If it does not, open:  http://127.0.0.1:8766/
echo.
echo   ------------------------------------------------------------

rem  Pass  n  to skip opening the browser:  start.bat n
set "OPENFLAG=--open"
if /i "%~1"=="n" set "OPENFLAG="

rem 8765 is reserved for the sibling project (monitor board): its virtual
rem orders live in the localStorage of the 8765 origin, so moving this
rem board onto 8765 would orphan them. This board is fixed to 8766; if
rem 8766 is taken, server.py pick_port keeps searching upward.
%PYEXE% server.py %OPENFLAG% --port 8766
set "RC=%ERRORLEVEL%"

echo   ------------------------------------------------------------
if "%RC%"=="3" echo   Already running - opened the existing board in your browser.
if not "%RC%"=="3" if not "%RC%"=="0" echo   [x] Server exited with code %RC%
if "%RC%"=="0" echo   Server stopped normally.
echo.
echo   Log: %CD%\data\server.log
echo.
pause
exit /b %RC%


:nopython
echo   [x] No usable Python found.
echo.
echo   Two common causes:
echo     1. You installed the Microsoft Store version - it is only a stub.
echo        Reinstall from  https://www.python.org/downloads/
echo     2. "Add Python to PATH" was not checked during installation.
echo.
echo   To verify, open a new Command Prompt and run:
echo       py -3 --version
echo       python --version
echo   Both must print an actual version number.
echo.
pause
exit /b 1
