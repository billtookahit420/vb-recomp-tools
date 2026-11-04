@echo off
rem VBRecomp Tools launcher (Windows). Uses an app-local virtual
rem environment, bootstrapping it on first run. The import check
rem runs BEFORE the venv is trusted (see the .sh launcher).
setlocal
cd /d "%~dp0"
set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

call :import_ok
if not errorlevel 1 goto run
where py >nul 2>&1
if errorlevel 1 (
    echo Python not found - install Python 3.10+ from python.org first.
    pause
    exit /b 1
)
if not exist "%PY%" (
    py -m venv "%VENV%"
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)
"%PY%" -m pip install --upgrade pip >nul 2>&1
"%PY%" -m pip install -r requirements.txt
call :import_ok
if errorlevel 1 (
    echo Dependencies still not importable after install -
    echo check the pip output above and your network connection.
    pause
    exit /b 1
)
:run
"%PY%" app\main.py %*
exit /b %errorlevel%

:import_ok
if not exist "%PY%" exit /b 1
"%PY%" -c "import PySide6, PIL" 2>nul
exit /b %errorlevel%
