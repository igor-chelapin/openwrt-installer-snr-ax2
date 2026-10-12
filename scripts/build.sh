#!/bin/sh
# Сборка для Linux и macOS. Нужен Python 3.9+ с tkinter
# (Debian/Ubuntu: sudo apt install python3-tk python3-venv; macOS: python.org или brew install python-tk).
set -e
cd "$(dirname "$0")/.."
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt pyinstaller
if [ "$(uname)" = Darwin ]; then
    pyinstaller --noconfirm --windowed --icon assets/app.icns --add-data 'firmware/*.itb:firmware' --add-data 'firmware/*.fip:firmware' --add-data 'firmware/SHA256SUMS:firmware' --name OpenWrtInstaller openwrt_installer.py
    echo "Готово: dist/OpenWrtInstaller.app"
else
    pyinstaller --noconfirm --onefile --add-data 'firmware/*.itb:firmware' --add-data 'firmware/*.fip:firmware' --add-data 'firmware/SHA256SUMS:firmware' --name OpenWrtInstaller openwrt_installer.py
    cp dist/OpenWrtInstaller ./OpenWrtInstaller
    echo "Готово: ./OpenWrtInstaller (запуск для прошивки: sudo -E ./OpenWrtInstaller)"
fi
# Панель управления — только управление (без прошивки)
if [ "$(uname)" = Darwin ]; then
    pyinstaller --windowed --icon assets/panel.icns --name OpenWrtPanel panel.py
else
    pyinstaller --onefile --name OpenWrtPanel panel.py && cp dist/OpenWrtPanel ./OpenWrtPanel
fi
