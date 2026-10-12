#!/bin/sh
# Запуск из исходников (Linux / macOS) после git clone:
#   ./run.sh          — настройка роутера, VPN, Wi-Fi и т. п.
#   sudo ./run.sh     — если нужно прошивать роутер (смена IP компьютера, TFTP)
# Первый запуск создаёт папку .venv и ставит зависимости (нужен интернет), дальше стартует сразу.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Не найден Python 3. Установите его: Debian/Ubuntu — sudo apt install python3 python3-venv python3-tk;"
    echo "Fedora — sudo dnf install python3 python3-tkinter; macOS — https://www.python.org/downloads/"
    exit 1
fi
if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "В Python нет tkinter (окно программы). Debian/Ubuntu: sudo apt install python3-tk python3-venv;"
    echo "Fedora: sudo dnf install python3-tkinter; macOS: поставьте Python с python.org."
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "Первый запуск: создаю .venv и ставлю зависимости…"
    "$PY" -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip
fi
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python "$PWD/openwrt_installer.py" "$@"
