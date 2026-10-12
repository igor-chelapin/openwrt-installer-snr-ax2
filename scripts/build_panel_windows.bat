@echo off
rem OpenWrt Panel (only router management, no flashing). Python 3.9+
cd /d "%~dp0\.."
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --noconsole --icon assets\panel.ico --name OpenWrtPanel panel.py
copy /Y dist\OpenWrtPanel.exe OpenWrtPanel.exe
pause
