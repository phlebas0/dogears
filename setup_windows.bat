@echo off
setlocal
cd /d "%~dp0"

rem Use the Windows Python launcher when available, but do not require it.
set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

rem Some Python installations have python.exe but no py.exe launcher.
if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo Cannot find Python 3.11 or newer on your PATH.
    echo Install Python from https://www.python.org/downloads/windows/
    echo During installation, enable "Add python.exe to PATH".
    echo Close and reopen this window before trying again.
    pause
    exit /b 1
)

echo Found compatible Python using: %PYTHON_CMD%
%PYTHON_CMD% -m venv .venv
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error

echo.
echo Setup complete. Double-click run_windows.bat to start dogears.
pause
exit /b 0

:error
echo Installation failed. See the error messages above.
pause
exit /b 1
