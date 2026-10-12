@echo off
rem Run from source on Windows after "git clone": creates .venv, installs dependencies, starts the program.
rem The program asks for administrator rights itself (needed only for flashing).
cd /d "%~dp0"

set REQ=requirements.txt
ver | find " 6.1." >nul && set REQ=requirements-win7.txt

if exist .venv\Scripts\python.exe goto :deps
set PY=
where py >nul 2>&1
if not errorlevel 1 set PY=py -3
if defined PY goto :mkvenv
where python >nul 2>&1
if not errorlevel 1 set PY=python
if defined PY goto :mkvenv
echo Python 3 not found. Install it from https://www.python.org/downloads/
echo (Windows 7: Python 3.8.10). Tick "Add python.exe to PATH" during installation.
pause
exit /b 1

:mkvenv
echo First run: creating .venv and installing dependencies...
%PY% -m venv .venv
if errorlevel 1 goto :fail
.venv\Scripts\python -m pip install -q --upgrade pip

:deps
.venv\Scripts\python -m pip install -q -r %REQ%
if errorlevel 1 goto :fail
start "" .venv\Scripts\pythonw.exe "%~dp0openwrt_installer.py" %*
exit /b 0

:fail
echo.
echo FAILED - see messages above.
pause
exit /b 1
