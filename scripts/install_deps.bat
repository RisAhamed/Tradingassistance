@echo off
REM Installs the project dependencies into the local .venv (Python 3.11).
REM Output is written to logs\pip_install.log for background monitoring.
cd /d "%~dp0\.."
if not exist logs mkdir logs
echo PIP_STARTED %DATE% %TIME% > logs\pip_install.log
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt >> logs\pip_install.log 2>&1
echo PIP_EXIT=%ERRORLEVEL% >> logs\pip_install.log
