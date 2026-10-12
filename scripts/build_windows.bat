@echo off
rem ================================================================
rem  OpenWrt Installer - Windows build (Windows 8 / 10 / 11; for Windows 7: build_windows.bat win7)
rem  Output folder "release":
rem    OpenWrtInstaller.exe              - single file, firmware is built in (just send it)
rem    OpenWrtInstaller-Setup-X.Y.Z.exe  - installer with shortcuts (needs Inno Setup 6)
rem  Needs Python 3.8+ (3.8.10 for the win7 build).
rem ================================================================
cd /d "%~dp0\.."

set PY=python
py -3.8 -c "pass" >nul 2>&1 && set PY=py -3.8

for /f "delims=" %%v in ('%PY% scripts\version.py') do set VER=%%v
echo Version: %VER%

rem "build_windows.bat win7" - libraries that still run on Windows 7 (no camera QR scan)
if /i "%~1"=="win7" (
  %PY% -m pip install -r requirements-win7.txt pyinstaller==5.13.2
) else (
  %PY% -m pip install -r requirements.txt pyinstaller
)

%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --uac-admin --icon assets\app.ico ^
  --add-data "firmware\*.itb;firmware" --add-data "firmware\*.fip;firmware" --add-data "firmware\SHA256SUMS;firmware" ^
  --name OpenWrtInstaller openwrt_installer.py
if errorlevel 1 goto :fail

if not exist release mkdir release
copy /Y dist\OpenWrtInstaller.exe release\OpenWrtInstaller.exe >nul

set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC goto :noiscc
%ISCC% /Q /DAppVersion=%VER% /DExeDir=..\dist installer\OpenWrtInstaller.iss
if errorlevel 1 goto :fail
echo.
echo DONE: release\OpenWrtInstaller-Setup-%VER%.exe  and  release\OpenWrtInstaller.exe
goto :end

:noiscc
echo.
echo DONE: release\OpenWrtInstaller.exe
echo For Setup.exe install Inno Setup 6 (https://jrsoftware.org/isdl.php) and run this script again.

:end
start "" release
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - see messages above.
pause
exit /b 1
