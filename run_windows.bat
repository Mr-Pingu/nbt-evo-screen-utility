@echo off
setlocal
cd /d "%~dp0"

set "PYTHON="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PYTHON=py -3"
if not defined PYTHON (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
    echo Python 3.9 or newer is required.
    pause
    exit /b 1
)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.9 or newer is required.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    %PYTHON% -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo Tkinter is required. Reinstall Python with Tcl/Tk support enabled.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import paramiko, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing missing Python dependencies from requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" hu_stream.py %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" pause
exit /b %RESULT%

:failed
echo Could not prepare or start the HU screen streamer.
pause
exit /b 1
