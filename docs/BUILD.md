# Сборка

## Автоматически (GitHub Actions)

Каждый push в `main` собирает все версии (артефакты во вкладке Actions). Каждая сборка проходит самопроверку: программа запускается с `--selftest`, находит встроенные прошивки и сверяет их SHA256 с `firmware/SHA256SUMS`. Тег `v*` (например `git tag v1.4.0 && git push origin v1.4.0`) создаёт Release со всеми файлами, архивом всего проекта и `SHA256SUMS.txt`:

| Сборка | Runner | Python |
|---|---|---|
| windows-x64 (10/11) | windows-2022 | 3.12 |
| windows7-x64 (7 SP1/8/8.1) | windows-2022 | 3.8 + `requirements-win7.txt`, PyInstaller 5.13.2 |
| linux-x64 | ubuntu-22.04 (glibc 2.35) | 3.12 |
| macos-arm64 | macos-14 | 3.12 |

## Вручную

- **Windows:** Python 3.8+ с python.org → `scripts\build_windows.bat` (для Windows 7: `scripts\build_windows.bat win7`, Python 3.8.10; для Python 3.8 на Win7 нужно обновление KB2533623).
  Результат — папка `release`: `OpenWrtInstaller.exe` (один файл, прошивки внутри) и, если установлен [Inno Setup 6](https://jrsoftware.org/isdl.php), установщик `OpenWrtInstaller-Setup-X.Y.Z.exe` (скрипт `installer/OpenWrtInstaller.iss`).
- **Linux / macOS:** `sh scripts/build.sh` (нужен tkinter: `sudo apt install python3-tk python3-venv`, на macOS — Python с python.org).

Прошивки из папки `firmware` встраиваются в программу при сборке (`--add-data`). Файлы прошивки, положенные рядом с программой или в подпапку `firmware` возле неё, имеют приоритет над встроенными.

## Почему для Windows 7 отдельная сборка

Python 3.9+ и новые версии `cryptography`/`bcrypt` (на Rust) на Windows 7 не запускаются. Сборка для Win7 использует Python 3.8 и версии библиотек без Rust. Сканирование QR камерой в ней отключено (без OpenCV), остальное работает так же.
