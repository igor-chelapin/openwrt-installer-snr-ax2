# -*- coding: utf-8 -*-
"""
OpenWrt installer for SNR AX2 (MediaTek Filogic).
Файлы прошивки встроены в exe (папка firmware при сборке). Свои файлы можно положить рядом
с программой или в подпапку firmware — они важнее встроенных:
  *bl31-uboot.fip, *initramfs-recovery.itb, *squashfs-sysupgrade.itb
Настройки, журналы и бэкапы хранятся в папке данных пользователя (см. user_data_dir).

Запускать от администратора (смена IP и правило брандмауэра).
"""
import os
import sys
import glob
import time
import queue
import socket
import struct
import hashlib
import gzip
import io
import zipfile
import tempfile
import shutil
import urllib.request
import urllib.error
import html
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import urlsplit, parse_qs, unquote
import ctypes
import threading
import subprocess
import shlex
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

PARAMIKO_ERROR = ""
try:
    import paramiko
except Exception as _e:          # не только ImportError: в сборке может не загрузиться двоичная библиотека
    paramiko = None
    PARAMIKO_ERROR = f"{type(_e).__name__}: {_e}"

BASE_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
if ".app/Contents/MacOS" in BASE_DIR:          # macOS: папка, где лежит OpenWrtInstaller.app
    BASE_DIR = os.path.dirname(BASE_DIR.split(".app/Contents/MacOS")[0])
# Ресурсы, встроенные в exe (прошивки): PyInstaller распаковывает их во временную папку sys._MEIPASS
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
APP_NAME = "OpenWrt Installer SNR AX2"
APP_VERSION = "1.4.0"
PANEL = False          # «Панель управления» — облегчённая версия только для управления роутером на OpenWrt (без прошивки)
REPO_URL = "https://github.com/igor-chelapin/panel-setup"

def user_data_dir(folder):
    """Скрытая папка для настроек, журналов и бэкапов — чтобы не мусорить рядом с exe (например на рабочем столе).
    Windows: %LOCALAPPDATA%\\<folder>; macOS: ~/Library/Application Support/<folder>; Linux: ~/.local/share/<folder>.
    Переносной режим: если рядом с программой лежит файл portable.txt — всё хранится рядом с ней, как раньше."""
    if os.path.exists(os.path.join(BASE_DIR, "portable.txt")):
        return BASE_DIR
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.expanduser("~/Library/Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = os.path.join(root, folder)
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return path
    except OSError:
        return BASE_DIR                       # нет доступа — работаем по-старому, рядом с программой


def _init_paths(folder, prefix=""):
    global DATA_DIR, BACKUP_DIR, LOG_FILE, SETTINGS_FILE, SUB_FILE, BLOCKCHECK_LOG
    DATA_DIR = user_data_dir(folder)
    BACKUP_DIR = DATA_DIR if DATA_DIR == BASE_DIR else os.path.join(DATA_DIR, "backups")
    LOG_FILE = os.path.join(DATA_DIR, prefix + ("log.txt" if prefix else "install_log.txt"))
    SETTINGS_FILE = os.path.join(DATA_DIR, prefix + "settings.json")
    SUB_FILE = os.path.join(DATA_DIR, "subscription.txt")
    BLOCKCHECK_LOG = os.path.join(DATA_DIR, "blockcheck_log.txt")


def migrate_legacy_files():
    """Один раз переносит settings.json и прочее из папки программы в папку данных. -> список перенесённого."""
    moved = []
    if DATA_DIR == BASE_DIR:
        return moved
    # старые версии клали эти файлы рядом с exe — переносим только файлы своей версии программы
    for name in {os.path.basename(x) for x in (SETTINGS_FILE, LOG_FILE, SUB_FILE, BLOCKCHECK_LOG)}:
        src, dst = os.path.join(BASE_DIR, name), os.path.join(DATA_DIR, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.move(src, dst)
                moved.append(name)
            except OSError:
                try:
                    shutil.copy2(src, dst)
                    moved.append(name)
                except OSError:
                    pass
    return moved


def find_backups():
    """Папки backup_* — и в папке данных, и рядом с программой (бэкапы старых версий)."""
    dirs = set()
    for d in (BACKUP_DIR, BASE_DIR, DATA_DIR):
        dirs.update(x for x in glob.glob(os.path.join(d, "backup_*")) if os.path.isdir(x))
    return sorted(dirs, key=lambda x: os.path.basename(x))


_init_paths("OpenWrtInstaller")


def set_edition(name):
    """Вызывается из panel.py до main(): включает «Панель управления» (без прошивки)."""
    global PANEL, APP_NAME
    if name == "panel":
        PANEL = True
        APP_NAME = "OpenWrt Панель управления"
        _init_paths("OpenWrtPanel", "panel_")


# Прошивки: сначала рядом с программой (можно подложить свои), потом встроенные в exe
FW_DIRS = [BASE_DIR, os.path.join(BASE_DIR, "firmware"), os.path.join(RES_DIR, "firmware")]
PATTERNS = {
    "fip": ["uboot.fip", "*bl31-uboot.fip", "*uboot*.fip"],
    "recovery": ["recovery.itb", "*initramfs-recovery.itb", "*recovery*.itb"],
    "sysupgrade": ["sysupgrade.itb", "*squashfs-sysupgrade.itb", "*sysupgrade*.itb"],
}
PC_IP = "192.168.1.254"
PC_MASK = "255.255.255.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0
FW_RULE = "OpenWrtInstaller"

WARNING_TEXT = (
    "ВНИМАНИЕ!\n\n"
    "• Во время прошивки НЕЛЬЗЯ отключать питание роутера и ноутбука.\n"
    "  Прерывание питания может превратить роутер в «кирпич».\n"
    "• Программа перепишет загрузчик (FIP) и сотрёт раздел ubi.\n"
    "• Если после перезагрузки роутер не скачает recovery по TFTP,\n"
    "  потребуется физическое вмешательство (UART/программатор).\n"
    "• Не отключайте LAN-кабель до конца процесса.\n\n"
    "Продолжить?"
)


class StepError(Exception):
    pass


# ---------------------------------------------------------------- Приём ссылки с телефона
try:
    import segno          # необязательно: рисует QR-код с адресом страницы
except ImportError:
    segno = None
try:
    import cv2            # необязательно: сканирование QR камерой ноутбука
except ImportError:
    cv2 = None

RECV_PORT = 8765
RECV_RULE = "OpenWrtInstallerRecv"
RECV_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Отправить ссылку</title>
<style>html,body{background:#fff;color:#000}
body{font-family:sans-serif;margin:16px;max-width:520px;font-size:18px;line-height:1.4}
h2{font-size:24px;color:#000}p{color:#000;font-weight:500}
textarea{width:100%;height:150px;font-size:17px;box-sizing:border-box;color:#000;background:#fff;
border:2px solid #000;border-radius:6px;padding:8px}
button{font-size:20px;font-weight:bold;padding:14px;margin-top:12px;width:100%;color:#fff;background:#000;
border:0;border-radius:8px}.m{margin-top:16px;font-size:19px;font-weight:bold;color:#000}</style>
</head><body><h2>Отправить ссылку на ноутбук</h2>
<p>Вставьте vless-ссылку (vless://...) или ссылку на подписку (https://...).</p>
<form method="post"><textarea name="link" placeholder="vless://...  или  https://..." required></textarea>
<button>Отправить</button></form><div class="m">@@MSG@@</div></body></html>"""


def local_ips(router_ip):
    """Все IPv4-адреса ноутбука; сначала те, что в подсети роутера (их увидит телефон)."""
    ips = []
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    try:
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.connect((router_ip, 80))
        ips.append(s_.getsockname()[0])
        s_.close()
    except OSError:
        pass
    ips = [i for i in dict.fromkeys(ips) if not i.startswith(("127.", "169.254."))]
    prefix = ".".join(router_ip.split(".")[:3]) + "."
    return sorted(ips, key=lambda i: (not i.startswith(prefix), i)) or [PC_IP]


def local_ip_towards(target):
    """IP ноутбука в сети роутера (его увидит телефон)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return PC_IP


class LinkReceiver:
    """Мини-сервер: принимает одну vless-ссылку с телефона по секретному адресу."""

    def __init__(self):
        self.token = ""      # адрес простой: http://IP:порт/
        self.result = None
        self.event = threading.Event()
        self.httpd = None
        self.hits = 0
        self.last_ip = ""

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _ok_path(self):
                return self.path.split("?")[0].rstrip("/") == ("/" + outer.token).rstrip("/")

            def do_GET(self):
                if not self._ok_path():
                    return self._send(404, "Not found")
                if self.client_address[0] != "127.0.0.1":      # самопроверку не считаем
                    outer.hits += 1
                    outer.last_ip = self.client_address[0]
                self._send(200, RECV_PAGE.replace("@@MSG@@", ""))

            def do_POST(self):
                if not self._ok_path():
                    return self._send(404, "Not found")
                n = int(self.headers.get("Content-Length") or 0)
                if n > 8192:
                    return self._send(413, "Too large")
                body = self.rfile.read(n).decode("utf-8", "replace")
                link = parse_qs(body).get("link", [""])[0].strip()
                try:
                    link_kind(link)
                except StepError as e:
                    msg = "❌ " + html.escape(str(e))
                    return self._send(400, RECV_PAGE.replace("@@MSG@@", msg))
                outer.result = link
                outer.event.set()
                self._send(200, RECV_PAGE.replace(
                    "@@MSG@@", "✅ Ссылка принята. Можно закрыть страницу."))

        self.httpd = ThreadingHTTPServer(("0.0.0.0", RECV_PORT), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass


# ---------------------------------------------------------------- Бэкап mtd
def connect_any(router, user, pw):
    """Вход с введёнными данными; если не вышло — пробует root без пароля (свежий OpenWrt)."""
    try:
        return Conn(router, user, [pw] if pw else [""])
    except StepError:
        if user == "root" and not pw:
            raise
        return Conn(router, "root", [""])


def parse_proc_mtd(text):
    parts = []
    for line in text.splitlines():
        m = re.match(r'^(mtd\d+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+"(.*)"', line.strip())
        if m:
            parts.append((m.group(1), int(m.group(2), 16), m.group(4)))
    return parts


def backup_mtd(cfg, log):
    adapter, router, user, pw = cfg["adapter"], cfg["router"], cfg["user"], cfg["password"]
    quick = cfg.get("quick")
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not is_admin():
        raise StepError(ADMIN_HINT)
    if not adapter:
        raise StepError("Выберите сетевой адаптер (тот, в который воткнут кабель роутера).")

    log(f"Бэкап 1/4: адаптер «{adapter}» -> {PC_IP}/{PC_MASK}")
    code, out = set_static_ip(adapter)
    if code != 0:
        raise StepError(f"Не удалось задать IP {PC_IP} на «{adapter}»: {out}")
    time.sleep(4)

    log(f"Бэкап 2/4: подключение к {router} по SSH")
    if not wait_ping(router, True, 30):
        raise StepError(f"Роутер {router} не отвечает. Проверьте кабель в LAN-порт и выбранный адаптер.")
    conn = connect_any(router, user, pw)
    try:
        code, out = conn.run("cat /proc/mtd")
        parts = parse_proc_mtd(out)
        if not parts:
            raise StepError("Не удалось прочитать список разделов (/proc/mtd).")
        if quick:
            parts = [p for p in parts if p[2] not in ("ubi", "spi0.0") and p[1] <= 32 * 1024 * 1024]
        has_gzip = conn.run("command -v gzip")[0] == 0
        os.makedirs(BACKUP_DIR, exist_ok=True)
        folder = os.path.join(BACKUP_DIR, "backup_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(folder, exist_ok=True)
        log(f"Бэкап 3/4: копирование разделов в {folder}")
        sums = []
        for dev, size, name in parts:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            path = os.path.join(folder, f"{dev}_{safe}" + (".bin.gz" if has_gzip else ".bin"))
            log(f"  {dev} «{name}» ({size / 1048576:.1f} МБ)")
            ch = conn.t.open_session()
            ch.settimeout(180)
            ch.exec_command(f"cat /dev/{dev} | gzip -1 -c" if has_gzip else f"cat /dev/{dev}")
            got, nextlog = 0, 16 * 1048576
            with open(path, "wb") as f:
                while True:
                    d = ch.recv(65536)
                    if not d:
                        break
                    f.write(d)
                    got += len(d)
                    if got >= nextlog:
                        log(f"    получено {got // 1048576} МБ")
                        nextlog += 16 * 1048576
            if ch.recv_exit_status() != 0 or got == 0:
                raise StepError(f"Не удалось прочитать {dev} «{name}». "
                                "Попробуйте режим «Быстрый бэкап» (без ubi и spi0.0).")
            total = got
            if has_gzip:
                total = 0
                try:
                    with gzip.open(path, "rb") as g:
                        while True:
                            c = g.read(1 << 20)
                            if not c:
                                break
                            total += len(c)
                except (OSError, EOFError):
                    raise StepError(f"{dev}: архив повреждён при передаче, повторите бэкап.")
            if total != size:
                raise StepError(f"{dev}: размер {total} не совпал с {size}. Повторите бэкап.")
            sums.append((md5_of(path), os.path.basename(path)))
            log("    OK")
        log("Бэкап 4/4: запись контрольных сумм")
        with open(os.path.join(folder, "MD5SUMS.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(f"{h}  {n}" for h, n in sums) + "\n")
            f.write("\n/proc/mtd:\n" + out + "\n")
        log(f"ГОТОВО. Бэкап в папке: {folder}")
        log("СКОПИРУЙТЕ эту папку в надёжное место (флешка, облако). Особенно важен Factory (калибровка Wi-Fi).")
        open_folder(folder)
    finally:
        conn.close()


# ---------------------------------------------------------------- VPN: маршрутизация
REDIR_PORT = 12345
DNS_PORT = 5353                  # DNS-запросы роутера идут через VPN
TEST_SOCKS_PORT = 10808          # локальный socks xray для проверки сервера сторожем

# YouTube и Discord: в режиме 3 их обслуживает zapret (напрямую), в режиме 2 — VPN
YT_DOMAINS = ["domain:youtube.com", "domain:youtu.be", "domain:youtube-nocookie.com",
              "domain:googlevideo.com", "domain:ytimg.com", "domain:ggpht.com",
              "domain:youtubei.googleapis.com", "domain:youtube.googleapis.com", "domain:yt.be"]
DC_DOMAINS = ["domain:discord.com", "domain:discord.gg", "domain:discordapp.com",
              "domain:discordapp.net", "domain:discord.media", "domain:discordcdn.com"]
# Сервисы Google/Microsoft, которые из РФ закрыты по региону — всегда через VPN
GEO_LOCKED = ["domain:gemini.google.com", "domain:aistudio.google.com", "domain:notebooklm.google.com",
              "domain:notebooklm.google", "domain:generativelanguage.googleapis.com", "domain:labs.google",
              "domain:bard.google.com", "domain:ai.google.dev", "domain:deepmind.google",
              "domain:alkalimakersuite-pa.clients6.google.com", "domain:proactivebackend-pa.googleapis.com",
              "domain:copilot.microsoft.com", "domain:sydney.bing.com", "domain:edgeservices.bing.com",
              "domain:copilot.cloud.microsoft"]
# Крупные сервисы, которые работают из РФ без VPN — напрямую, чтобы не грузить сервер
DIRECT_GEOSITES = ["geosite:google", "geosite:apple", "geosite:microsoft"]


def build_xray_config(link):
    """vless://... -> конфиг xray (dict). Бросает StepError при проблемах."""
    link = link.strip()
    try:
        u = urlsplit(link)
        host, port, uuid = u.hostname, u.port, unquote(u.username or "")
    except ValueError:
        raise StepError("Не удалось разобрать vless-ссылку.")
    if u.scheme != "vless" or not (host and port and uuid):
        raise StepError("Некорректная ссылка. Формат: vless://UUID@host:port?параметры#имя")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    net = q.get("type", "tcp")
    sec = q.get("security", "none")
    ss = {"network": net, "security": sec}
    path = unquote(q.get("path", "/"))
    hosthdr = q.get("host", "")
    if net == "ws":
        ws = {"path": path}
        if hosthdr:
            ws["headers"] = {"Host": hosthdr}
        ss["wsSettings"] = ws
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": unquote(q.get("serviceName", "")),
                              "multiMode": q.get("mode") == "multi"}
    elif net in ("xhttp", "splithttp"):
        ss["network"] = "xhttp"
        xh = {"path": path, "mode": q.get("mode", "auto")}
        if hosthdr:
            xh["host"] = hosthdr
        if q.get("extra"):
            try:
                xh["extra"] = json.loads(unquote(q["extra"]))
            except ValueError:
                raise StepError("Параметр extra в ссылке — некорректный JSON.")
        ss["xhttpSettings"] = xh
    elif net == "httpupgrade":
        hu = {"path": path}
        if hosthdr:
            hu["host"] = hosthdr
        ss["httpupgradeSettings"] = hu
    elif net != "tcp":
        raise StepError(f"Транспорт «{net}» не поддерживается этой программой.")
    if sec == "tls":
        tls = {"serverName": q.get("sni") or hosthdr or host,
               "fingerprint": q.get("fp", "chrome")}
        if q.get("alpn"):
            tls["alpn"] = unquote(q["alpn"]).split(",")
        ss["tlsSettings"] = tls
    elif sec == "reality":
        if not q.get("pbk"):
            raise StepError("В ссылке Reality нет параметра pbk (публичный ключ).")
        ss["realitySettings"] = {
            "serverName": q.get("sni", ""), "fingerprint": q.get("fp", "chrome"),
            "publicKey": q["pbk"], "shortId": q.get("sid", ""),
            "spiderX": unquote(q.get("spx", ""))}
    elif sec != "none":
        raise StepError(f"Тип защиты «{sec}» не поддерживается.")
    user = {"id": uuid, "encryption": q.get("encryption", "none")}
    if q.get("flow"):
        user["flow"] = q["flow"]
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "redir", "listen": "0.0.0.0", "port": REDIR_PORT,
            "protocol": "dokodemo-door",
            "settings": {"network": "tcp", "followRedirect": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}}],
        "outbounds": [
            {"tag": "proxy", "protocol": "vless",
             "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
             "streamSettings": ss},
            {"tag": "direct", "protocol": "freedom"}],
    }


# ---------------------------------------------------------------- Подписки
SUB_UA = "v2rayN/7.0"


def is_sub_url(s):
    return bool(re.fullmatch(r"https?://\S+", s.strip()))


def link_kind(s):
    """'vless' | 'sub' — иначе StepError."""
    s = s.strip()
    if is_sub_url(s):
        return "sub"
    build_xray_config(s)
    return "vless"


def parse_subscription(text):
    """Текст подписки (base64 или построчно) -> ([(имя, адрес, тип, ссылка)], пропущено)."""
    t = text.strip().lstrip("\ufeff")
    if "://" not in t[:300]:
        raw = re.sub(r"\s+", "", t).replace("-", "+").replace("_", "/")
        try:
            t = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
        except ValueError:
            pass
    servers, skipped = [], 0
    for line in t.splitlines():
        line = line.strip()
        if not line.startswith("vless://"):
            if "://" in line:
                skipped += 1
            continue
        try:
            build_xray_config(line)
        except StepError:
            skipped += 1
            continue
        u = urlsplit(line)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        addr = f"{u.hostname}:{u.port}"
        # эмодзи-флаги убираем: старый Tk в Python 3.8 падает на символах вне BMP
        name = "".join(c for c in unquote(u.fragment) if ord(c) < 0x10000).strip() or addr
        servers.append((name, addr, f"{q.get('type', 'tcp')}/{q.get('security', 'none')}", line))
    return servers, skipped


def fetch_subscription(cfg, log):
    """Скачивает подписку: сначала с ноутбука, если не вышло — через роутер. Возвращает список серверов."""
    url = cfg["sub_url"].strip()
    if not is_sub_url(url):
        raise StepError("Ссылка на подписку должна начинаться с https:// (или http://).")
    text = None
    log("Подписка: загрузка списка серверов")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": SUB_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read(4_000_000).decode("utf-8", "replace")
        log("  загружено с ноутбука")
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"  с ноутбука не вышло ({e}), пробую через роутер")
    if text is None:
        if paramiko is None:
            raise StepError("Не установлен paramiko: pip install paramiko")
        if not wait_ping(cfg["router"], True, 10):
            raise StepError("Подписка не загрузилась: у ноутбука нет интернета, а роутер не отвечает.")
        conn = connect_any(cfg["router"], cfg["user"], cfg["password"])
        try:
            code, text = conn.run(f"curl -fsSL -m 25 -A {shq(SUB_UA)} {shq(url)} 2>/dev/null || "
                                  f"uclient-fetch -q -T 25 -U {shq(SUB_UA)} -O - {shq(url)} 2>/dev/null", timeout=60)
        finally:
            conn.close()
        if code != 0 or not text.strip():
            raise StepError("Подписка не загрузилась ни с ноутбука, ни с роутера. Проверьте ссылку и интернет.")
        log("  загружено через роутер")
    servers, skipped = parse_subscription(text)
    if not servers:
        raise StepError("В подписке не найдено ни одного vless-сервера"
                        + (f" (пропущено ссылок других типов: {skipped})" if skipped else "") + ".\n"
                        "Возможно, сервис отдаёт подписку в своём формате (Clash/Happ-шифрование) — "
                        "тогда скопируйте из VPN-приложения отдельную vless-ссылку.")
    log(f"  серверов vless: {len(servers)}" + (f", пропущено других: {skipped}" if skipped else ""))
    try:
        with open(SUB_FILE, "w", encoding="utf-8") as f:
            f.write(url)
    except OSError:
        pass
    return servers


def parse_user_list(text):
    """Пользовательский список: домены / IP / CIDR через пробел, запятую или с новой строки.
    Возвращает (domains, ips) в формате xray."""
    domains, ips = [], []
    for raw in re.split(r"[\s,;]+", text or ""):
        s = raw.strip().lower()
        if not s or s.startswith("#"):
            continue
        if s.startswith(("geosite:", "geoip:")):
            (ips if s.startswith("geoip:") else domains).append(s)
            continue
        if not re.fullmatch(r"[\d.]+(/\d+)?", s):
            s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split(":")[0]
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?", s):
            ips.append(s)
        elif re.fullmatch(r"\*?\.?[a-z0-9-]+(\.[a-z0-9-]+)+", s):
            domains.append("domain:" + s.lstrip("*."))
    return list(dict.fromkeys(domains)), list(dict.fromkeys(ips))


def build_routing(zapret_mode, direct_geosites=None, yt_via_vpn=False, user_direct="", user_proxy=""):
    """Правила xray.
    zapret_mode — YouTube/Discord идут напрямую через zapret (режим 3);
    yt_via_vpn  — запасной вариант режима 3: zapret не справился, YouTube/Discord временно через VPN."""
    sites = DIRECT_GEOSITES if direct_geosites is None else direct_geosites
    rules = [
        {"type": "field", "inboundTag": ["socks-test", "dns-in"], "outboundTag": "proxy"},
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
    ]
    for text, tag in ((user_proxy, "proxy"), (user_direct, "direct")):
        d, i = parse_user_list(text)
        if d:
            rules.append({"type": "field", "domain": d, "outboundTag": tag})
        if i:
            rules.append({"type": "field", "ip": i, "outboundTag": tag})
    rules.append({"type": "field", "protocol": ["bittorrent"], "outboundTag": "direct"})
    rules.append({"type": "field", "domain": list(GEO_LOCKED), "outboundTag": "proxy"})
    if zapret_mode and not yt_via_vpn:
        rules.append({"type": "field", "domain": YT_DOMAINS + DC_DOMAINS, "outboundTag": "direct"})
    else:
        rules.append({"type": "field", "domain": YT_DOMAINS + DC_DOMAINS, "outboundTag": "proxy"})
    rules.append({"type": "field", "domain": ["geosite:category-ru"] + list(sites), "outboundTag": "direct"})
    rules.append({"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"})
    rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "proxy"})
    return {"routing": {"domainStrategy": "IPIfNonMatch", "rules": rules}}


def build_base_config():
    """Входы xray: прозрачный (трафик LAN) и локальный socks для проверки сервера сторожем."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "redir", "listen": "0.0.0.0", "port": REDIR_PORT, "protocol": "dokodemo-door",
             "settings": {"network": "tcp", "followRedirect": True},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"tag": "socks-test", "listen": "127.0.0.1", "port": TEST_SOCKS_PORT, "protocol": "socks",
             "settings": {"udp": False}},
            # DNS через VPN: провайдер не может подменить ответы
            {"tag": "dns-in", "listen": "127.0.0.1", "port": DNS_PORT, "protocol": "dokodemo-door",
             "settings": {"address": "1.1.1.1", "port": 53, "network": "tcp,udp"}}],
    }


def build_outbounds(link):
    c = build_xray_config(link)
    for ob in c["outbounds"]:
        if ob.get("tag") == "direct":
            ob["settings"] = {"domainStrategy": "UseIPv4"}
    return {"outbounds": c["outbounds"]}


def link_name(link):
    u = urlsplit(link.strip())
    name = "".join(c for c in unquote(u.fragment) if ord(c) < 0x10000).strip()
    return name or f"{u.hostname}:{u.port}"


def build_nft_rules_all():
    """На xray уходит весь TCP из LAN, кроме локальных адресов и портов торрентов.
    Что идёт напрямую (РФ, Google, Apple, Microsoft, торренты), а что в VPN — решает xray."""
    return f"""table inet xray {{
    set bypass {{
        type ipv4_addr
        flags interval
        elements = {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 }}
    }}
    chain prerouting {{
        type nat hook prerouting priority dstnat; policy accept;
        iifname "br-lan" udp dport 53 redirect to :53
        iifname "br-lan" tcp dport 53 redirect to :53
        iifname "br-lan" ip daddr @bypass return
        iifname "br-lan" tcp dport {{ 6881-6999, 51413 }} return
        iifname "br-lan" meta nfproto ipv4 meta l4proto tcp redirect to :{REDIR_PORT}
    }}
    chain quic_block {{
        type filter hook forward priority filter; policy accept;
        iifname "br-lan" ip daddr != @bypass udp dport 443 reject
    }}
}}
"""


XD = "/etc/xray-vpn"
XRAY_FILES = f"-c {XD}/base.json -c {XD}/outbound.json -c {XD}/routing.json"

INIT_SCRIPT = f"""#!/bin/sh /etc/rc.common
START=99
STOP=10
USE_PROCD=1

start_service() {{
    nft delete table inet xray 2>/dev/null
    nft -f {XD}/rules.nft
    [ -f /etc/dnsctl/dnsctl.sh ] && sh /etc/dnsctl/dnsctl.sh apply vpn-on
    procd_open_instance
    procd_set_param command /usr/bin/xray run {XRAY_FILES}
    ASSET=$(cat {XD}/asset-dir 2>/dev/null)
    [ -n "$ASSET" ] && procd_set_param env XRAY_LOCATION_ASSET="$ASSET"
    procd_set_param respawn
    procd_set_param stderr 1
    procd_set_param stdout 1
    procd_close_instance
}}

stop_service() {{
    nft delete table inet xray 2>/dev/null
    [ -f /etc/dnsctl/dnsctl.sh ] && sh /etc/dnsctl/dnsctl.sh apply vpn-off
}}
"""

# Сторож (cron, раз в 3 минуты):
#  1) сервер VPN не отвечает 2 проверки подряд -> следующий сервер из сохранённых;
#  2) режим 3: YouTube сначала через zapret; если zapret не справляется 2 проверки подряд —
#     YouTube и Discord временно через VPN; как только zapret снова тянет — обратно на zapret.
WATCHDOG_SH = r"""#!/bin/sh
D=/etc/xray-vpn
[ -e /etc/rc.d/S99xray-vpn ] || exit 0
[ -f $D/watchdog.conf ] && . $D/watchdog.conf
LOCK=/tmp/xray-watchdog.lock
[ -e $LOCK ] && exit 0
touch $LOCK
trap 'rm -f $LOCK' EXIT
log() { logger -t xray-watchdog "$*"; }
code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$@" 2>/dev/null; }
cnt() { n=$(cat /tmp/xwd_$1 2>/dev/null); n=$(( ${n:-0} + 1 )); echo $n > /tmp/xwd_$1; echo $n; }
rst() { rm -f /tmp/xwd_$1; }
# задержка (TCP-подключение) до каждого сервера: "NN мс", 0 — не отвечает
lat_all() {
    for f in $D/servers/*.json; do
        [ -f "$f" ] || continue
        n=${f##*/}; n=${n%.json}
        h=$(jsonfilter -i "$f" -e '@.outbounds[0].settings.vnext[0].address' 2>/dev/null)
        p=$(jsonfilter -i "$f" -e '@.outbounds[0].settings.vnext[0].port' 2>/dev/null)
        [ -n "$h" ] && [ -n "$p" ] || continue
        case "$h" in *:*) h="[$h]" ;; esac
        ( t=$(curl -s -o /dev/null -m 3 -w '%{time_connect}' "http://$h:$p/" 2>/dev/null); echo "$n ${t:-0}" ) &
    done
    wait
}
# самый быстрый отвечающий сервер, кроме текущего: "NN мс"
best_other() {
    echo "$1" | awk -v c="$2" '$1!=c && $2>0 {ms=int($2*1000); if (b=="" || ms<bm) {b=$1; bm=ms}} END {if (b!="") print b, bm}'
}
switch_to() {
    old=$(cat $D/current 2>/dev/null)
    cp $D/servers/$1.json $D/outbound.json && echo $1 > $D/current
    log "$2: «$(cat $D/servers/$old.name 2>/dev/null)» -> «$(cat $D/servers/$1.name 2>/dev/null)»"
    restart=1
}
command -v curl >/dev/null || exit 0
restart=0
c=$(code https://ya.ru)
case "$c" in 2*|3*) ;; *) exit 0 ;; esac
if ! pidof xray >/dev/null; then /etc/init.d/xray-vpn restart; exit 0; fi

# режим "latency" (cron раз в 15 минут): уйти с медленного сервера на заметно более быстрый
if [ "$1" = latency ]; then
    [ "${LAT_SWITCH:-0}" = 1 ] && [ "${FAILOVER:-0}" = 1 ] || exit 0
    cur=$(cat $D/current 2>/dev/null)
    res=$(lat_all)
    cm=$(echo "$res" | awk -v c="$cur" '$1==c {print int($2*1000)}')
    bl=$(best_other "$res" "$cur")
    [ -n "$bl" ] || exit 0
    b=${bl% *}; bm=${bl#* }
    e2e=$(curl -s -o /dev/null -m 8 -w '%{time_total}' -x socks5h://127.0.0.1:10808 \
          https://www.gstatic.com/generate_204 2>/dev/null | awk '{print int($1*1000)}')
    slow=0
    [ "${cm:-0}" -gt 0 ] && [ "$cm" -gt $((bm * 2)) ] && [ $((cm - bm)) -ge "${LAT_DIFF:-80}" ] && slow=1
    [ "${e2e:-0}" -ge "${E2E_MAX:-3000}" ] && slow=1
    echo "$(date '+%d.%m %H:%M') текущий ${cm:-?} мс (отклик через VPN ${e2e:-?} мс), лучший ${bm} мс" > $D/latency.last
    # не скакать между серверами: после переключения по задержке ждём 2 часа
    ts=$(cat $D/lat_switch.ts 2>/dev/null)
    [ -n "$ts" ] && [ $(( $(date +%s) - ts )) -lt 7200 ] && slow=0
    if [ $slow = 1 ]; then
        [ "$(cnt lat)" -ge 2 ] && { rst lat; date +%s > $D/lat_switch.ts
            switch_to "$b" "большая задержка (${cm:-?} мс, через VPN ${e2e:-?} мс), лучший ${bm} мс"; }
    else
        rst lat
    fi
    [ $restart = 1 ] && /etc/init.d/xray-vpn restart
    exit 0
fi

if [ "${FAILOVER:-0}" = 1 ]; then
    c=$(code -x socks5h://127.0.0.1:10808 https://www.gstatic.com/generate_204)
    if [ "$c" = 204 ]; then
        rst vpn
    elif [ "$(cnt vpn)" -ge 2 ]; then
        rst vpn
        list=$(ls $D/servers/ 2>/dev/null | sed -n 's/\.json$//p' | sort)
        if [ "$(echo "$list" | grep -c .)" -gt 1 ]; then
            cur=$(cat $D/current 2>/dev/null)
            bl=$(best_other "$(lat_all)" "$cur")
            next=${bl% *}
            if [ -z "$next" ]; then
                next=$(echo "$list" | awk -v c="$cur" 'f{print;exit} $0==c{f=1}')
                [ -n "$next" ] || next=$(echo "$list" | head -n 1)
            fi
            switch_to "$next" "сервер не отвечает"
        fi
    fi
fi

if [ "${ZAPRET_MODE:-0}" = 1 ] && [ -f $D/routing-ytvpn.json ]; then
    st=$(cat $D/yt_state 2>/dev/null)
    ok=0
    if pidof nfqws >/dev/null; then
        r=$(curl -s -o /dev/null -m 20 -w '%{http_code} %{size_download}' https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg 2>/dev/null)
        sz=${r#* }; sz=${sz%%.*}; sz=${sz%%,*}
        case "$sz" in ''|*[!0-9]*) sz=0 ;; esac
        [ "${r%% *}" = 200 ] && [ "$sz" -ge 25000 ] && ok=1
    fi
    if [ "$st" != vpn ]; then
        if [ $ok = 1 ]; then
            rst yt
        elif [ "$(cnt yt)" -ge 2 ]; then
            rst yt
            cp $D/routing-ytvpn.json $D/routing.json && echo vpn > $D/yt_state
            log "zapret не справляется с YouTube — YouTube и Discord временно через VPN"
            restart=1
        fi
    else
        if [ $ok = 1 ]; then
            if [ "$(cnt ytok)" -ge 2 ]; then
                rst ytok
                cp $D/routing-main.json $D/routing.json && echo zapret > $D/yt_state
                log "zapret снова работает — YouTube и Discord опять через zapret"
                restart=1
            fi
        else
            rst ytok
        fi
    fi
fi
[ $restart = 1 ] && /etc/init.d/xray-vpn restart
exit 0
"""

GEO_UPDATE_SH = r"""#!/bin/sh
# Обновление баз geoip/geosite (v2fly). Новые файлы проверяются xray до замены.
D=/etc/xray-vpn
G=$D/geo
T=/tmp/geo-new
rm -rf $T; mkdir -p $T $G
dl() { uclient-fetch -q -T 60 -O "$T/$2" "$1" 2>/dev/null || curl -fsSL -m 180 -o "$T/$2" "$1" 2>/dev/null; }
dl https://github.com/v2fly/geoip/releases/latest/download/geoip.dat geoip.dat
dl https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat geosite.dat
if [ ! -s $T/geoip.dat ] || [ ! -s $T/geosite.dat ]; then echo "ОШИБКА: не скачалось (нет доступа к GitHub?)"; rm -rf $T; exit 1; fi
need=$(( ( $(wc -c < $T/geoip.dat) + $(wc -c < $T/geosite.dat) ) / 1024 ))
have=0
[ -s $G/geoip.dat ] && have=$(( ( $(wc -c < $G/geoip.dat) + $(wc -c < $G/geosite.dat 2>/dev/null || echo 0) ) / 1024 ))
free=$(df -k /overlay | awk 'NR==2{print $4}')
if [ $(( ${free:-0} + have )) -lt $(( need + 3072 )) ]; then echo "ОШИБКА: мало места (свободно ${free} КБ, нужно ${need} КБ)"; rm -rf $T; exit 2; fi
if [ -s $D/base.json ] && ! XRAY_LOCATION_ASSET=$T /usr/bin/xray run -test -c $D/base.json -c $D/outbound.json -c $D/routing.json >/dev/null 2>&1; then
    echo "ОШИБКА: новые базы не подходят к текущим правилам, оставляю старые"; rm -rf $T; exit 3
fi
mv -f $T/geoip.dat $G/geoip.dat && mv -f $T/geosite.dat $G/geosite.dat
rm -rf $T
echo $G > $D/asset-dir
logger -t xray-vpn "geo-базы обновлены"
echo "OK: базы обновлены ($need КБ)"
[ -e /etc/rc.d/S99xray-vpn ] && /etc/init.d/xray-vpn restart
exit 0
"""

# Автообновление подписки на самом роутере (ucode есть в OpenWrt по умолчанию).
# Разбор vless-ссылок повторяет build_xray_config один в один.
SUB_UPDATE_UC = r"""#!/usr/bin/ucode
// Автообновление подписки VPN на роутере: скачивает список, разбирает vless-ссылки,
// обновляет /etc/xray-vpn/servers, сохраняет текущий сервер (по имени/адресу), проверяет xray.
import * as fs from 'fs';

const D = getenv('XD') || '/etc/xray-vpn';
const UA = 'v2rayN/7.0';
const SOCKS = 'socks5h://127.0.0.1:10808';

function log(msg) {
	print(msg, '\n');
	if (!getenv('XD'))
		system(['logger', '-t', 'xray-sub', msg]);
}

function sh_quote(s) {
	return "'" + replace(s, "'", "'\\''") + "'";
}

function urldec(s) {
	if (s == null) return null;
	return replace(s, /%([0-9A-Fa-f]{2})/g, (m, h) => chr(hex(h)));
}

function fetch(url) {
	let cmds = [];
	if (getenv('SUB_FETCH_CMD'))
		push(cmds, getenv('SUB_FETCH_CMD') + ' ' + sh_quote(url));
	else {
		push(cmds, 'curl -fsSL -m 30 -A ' + sh_quote(UA) + ' -x ' + SOCKS + ' ' + sh_quote(url) + ' 2>/dev/null');
		push(cmds, 'curl -fsSL -m 30 -A ' + sh_quote(UA) + ' ' + sh_quote(url) + ' 2>/dev/null');
		push(cmds, 'uclient-fetch -q -T 30 -U ' + sh_quote(UA) + ' -O - ' + sh_quote(url) + ' 2>/dev/null');
	}
	for (let c in cmds) {
		let p = fs.popen(c, 'r');
		if (!p) continue;
		let out = p.read('all');
		p.close();
		if (out && length(trim(out)) > 0)
			return out;
	}
	return null;
}

function decode_list(text) {
	let t = trim(text);
	if (index(substr(t, 0, 300), '://') < 0) {
		let raw = replace(replace(replace(t, /\s+/g, ''), /-/g, '+'), /_/g, '/');
		while (length(raw) % 4) raw += '=';
		let d = b64dec(raw);
		if (d) t = d;
	}
	return split(t, /\r?\n/);
}

// vless://uuid@host:port?params#name  ->  { name, address, outbounds } или null
function parse_vless(link) {
	let m = match(link, /^vless:\/\/([^@]+)@(\[[^\]]+\]|[^:\/?#]+):([0-9]+)\/?(\?[^#]*)?(#.*)?$/);
	if (!m) return null;
	let uuid = urldec(m[1]);
	let host = lc(replace(replace(m[2], /^\[/, ''), /\]$/, ''));
	let port = int(m[3]);
	let q = {};
	if (m[4])
		for (let kv in split(substr(m[4], 1), '&')) {
			let i = index(kv, '=');
			if (i > 0 && !exists(q, substr(kv, 0, i)))
				q[substr(kv, 0, i)] = urldec(substr(kv, i + 1));
		}
	let name = m[5] ? urldec(substr(m[5], 1)) : '';
	let net = q.type || 'tcp', sec = q.security || 'none';
	let ss = { network: net, security: sec };
	let path = q.path || '/', hosthdr = q.host || '';
	if (net == 'ws') {
		let ws = { path: path };
		if (hosthdr) ws.headers = { Host: hosthdr };
		ss.wsSettings = ws;
	}
	else if (net == 'grpc')
		ss.grpcSettings = { serviceName: q.serviceName || '', multiMode: q.mode == 'multi' };
	else if (net == 'xhttp' || net == 'splithttp') {
		ss.network = 'xhttp';
		let xh = { path: path, mode: q.mode || 'auto' };
		if (hosthdr) xh.host = hosthdr;
		if (q.extra) {
			let e = json(q.extra);
			if (e == null) return null;
			xh.extra = e;
		}
		ss.xhttpSettings = xh;
	}
	else if (net == 'httpupgrade') {
		let hu = { path: path };
		if (hosthdr) hu.host = hosthdr;
		ss.httpupgradeSettings = hu;
	}
	else if (net != 'tcp')
		return null;
	if (sec == 'tls') {
		let tls = { serverName: q.sni || hosthdr || host, fingerprint: q.fp || 'chrome' };
		if (q.alpn) tls.alpn = split(q.alpn, ',');
		ss.tlsSettings = tls;
	}
	else if (sec == 'reality') {
		if (!q.pbk) return null;
		ss.realitySettings = { serverName: q.sni || '', fingerprint: q.fp || 'chrome',
			publicKey: q.pbk, shortId: q.sid || '', spiderX: q.spx || '' };
	}
	else if (sec != 'none')
		return null;
	let user = { id: uuid, encryption: q.encryption || 'none' };
	if (q.flow) user.flow = q.flow;
	// эмодзи-флаги (4-байтовые символы) убираем из имени
	name = trim(replace(name, /[\xf0-\xf4][\x80-\xbf][\x80-\xbf][\x80-\xbf]/g, ''));
	return {
		name: name || (host + ':' + port),
		address: host + ':' + port,
		outbounds: { outbounds: [
			{ tag: 'proxy', protocol: 'vless',
			  settings: { vnext: [ { address: host, port: port, users: [ user ] } ] },
			  streamSettings: ss },
			{ tag: 'direct', protocol: 'freedom', settings: { domainStrategy: 'UseIPv4' } }
		] }
	};
}

function read(path) {
	let s = fs.readfile(path);
	return s != null ? trim(s) : '';
}

function pad2(n) {
	return n < 10 ? '0' + n : '' + n;
}

// ---------------------------------------------------------------- main
let url = read(D + '/sub.url');
if (!url) {
	log('подписка не задана — нечего обновлять');
	exit(0);
}
let text = fetch(url);
if (!text) {
	log('не удалось скачать подписку — серверы оставлены прежними');
	exit(1);
}
let excl = {};
for (let n in split(read(D + '/exclude.names'), /\r?\n/))
	if (trim(n)) excl[trim(n)] = 1;
let servers = [], skipped = 0, nexcl = 0;
for (let line in decode_list(text)) {
	line = trim(line);
	if (index(line, 'vless://') != 0) {
		if (index(line, '://') > 0) skipped++;
		continue;
	}
	let s = parse_vless(line);
	if (!s) skipped++;
	else if (excl[s.name]) nexcl++;
	else push(servers, s);
}
if (!length(servers)) {
	log('в подписке нет подходящих vless-серверов (исключено: ' + nexcl + ') — серверы оставлены прежними');
	exit(1);
}

// текущий сервер — запоминаем по имени, потом по адресу
let cur = read(D + '/current');
let cur_name = cur ? read(D + '/servers/' + cur + '.name') : '';
let cur_addr = '';
let co = json(read(D + '/outbound.json') || 'null');
if (co && co.outbounds && co.outbounds[0] && co.outbounds[0].settings && co.outbounds[0].settings.vnext)
	cur_addr = co.outbounds[0].settings.vnext[0].address + ':' + co.outbounds[0].settings.vnext[0].port;
let pick = 0;
for (let i = 0; i < length(servers); i++)
	if (cur_name && servers[i].name == cur_name) { pick = i; break; }
if (servers[pick].name != cur_name)
	for (let i = 0; i < length(servers); i++)
		if (cur_addr && servers[i].address == cur_addr) { pick = i; break; }

// новый каталог серверов
let NEW = D + '/servers.new';
system('rm -rf ' + NEW + ' && mkdir -p ' + NEW);
let hosts = {};
for (let i = 0; i < length(servers); i++) {
	let n = pad2(i + 1);
	fs.writefile(NEW + '/' + n + '.json', sprintf('%.J\n', servers[i].outbounds));
	fs.writefile(NEW + '/' + n + '.name', servers[i].name + '\n');
	let h = servers[i].outbounds.outbounds[0].settings.vnext[0].address;
	if (!match(h, /^[0-9.]+$/) && match(h, /^[a-z0-9.-]+$/)) hosts[h] = 1;
}
let new_out = sprintf('%.J\n', servers[pick].outbounds);
let old_out = fs.readfile(D + '/outbound.json') || '';

// проверка xray с выбранным сервером
fs.writefile(D + '/outbound.test.json', new_out);
let asset = read(D + '/asset-dir');
let rc = getenv('XD') ? 0 : system('XRAY_LOCATION_ASSET=' + asset + ' /usr/bin/xray run -test -c ' + D +
	'/base.json -c ' + D + '/outbound.test.json -c ' + D + '/routing.json >/dev/null 2>&1');
fs.unlink(D + '/outbound.test.json');
if (rc != 0) {
	system('rm -rf ' + NEW);
	log('xray не принял сервер из обновлённой подписки — оставляю прежние');
	exit(1);
}
system('rm -rf ' + D + '/servers.old; [ -d ' + D + '/servers ] && mv ' + D + '/servers ' + D + '/servers.old; mv ' + NEW + ' ' + D + '/servers; rm -rf ' + D + '/servers.old');
fs.writefile(D + '/current', pad2(pick + 1) + '\n');
fs.writefile(D + '/dns-hosts', join('\n', sort(keys(hosts))) + '\n');
fs.writefile(D + '/sub.updated', sprintf('%s|%d\n', trim(fs.popen('date "+%d.%m %H:%M"', 'r').read('all')), length(servers)));
system('sed -i "s/^FAILOVER=.*/FAILOVER=' + (length(servers) > 1 ? 1 : 0) + '/" ' + D + '/watchdog.conf 2>/dev/null');

let old_hosts = read(D + '/dns-hosts.prev');
let new_hosts = trim(join('\n', sort(keys(hosts))));
fs.writefile(D + '/dns-hosts.prev', new_hosts + '\n');
if (new_out != old_out) {
	fs.writefile(D + '/outbound.json', new_out);
	log(sprintf('подписка обновлена: серверов %d, текущий «%s» — ключ изменился, перезапускаю VPN',
		length(servers), servers[pick].name));
	if (!getenv('XD') && fs.access('/etc/rc.d/S99xray-vpn'))
		system('/etc/init.d/xray-vpn restart');
}
else {
	log(sprintf('подписка обновлена: серверов %d, текущий «%s» без изменений', length(servers), servers[pick].name));
	if (old_hosts != new_hosts && !getenv('XD') && fs.access('/etc/dnsctl/dnsctl.sh'))
		system('sh /etc/dnsctl/dnsctl.sh apply');
}
exit(0);
"""

CRON_SETUP = (f"touch /etc/crontabs/root; grep -v '{XD}/' /etc/crontabs/root > /tmp/cron.new; "
              f"echo '*/3 * * * * {XD}/watchdog.sh' >> /tmp/cron.new; "
              f"echo '30 4 * * 0 {XD}/geo-update.sh >/dev/null 2>&1' >> /tmp/cron.new; "
              f"echo '17 */6 * * * /usr/bin/ucode {XD}/sub-update.uc >/dev/null 2>&1' >> /tmp/cron.new; "
              f"echo '7,22,37,52 * * * * {XD}/watchdog.sh latency' >> /tmp/cron.new; "
              "cp /tmp/cron.new /etc/crontabs/root; rm -f /tmp/cron.new; "
              "/etc/init.d/cron enable; /etc/init.d/cron restart; true")

# убирает следы старых версий программы (режим «по списку доменов», единый config.json)
OLD_LIST_CLEANUP = (f"if [ -f {XD}/dnsmasq.list ]; then "
                    f"[ -f {XD}/dns-remove.sh ] && sh {XD}/dns-remove.sh; "
                    f"rm -f {XD}/dnsmasq.list {XD}/dns-apply.sh {XD}/dns-remove.sh "
                    "/tmp/dnsmasq.d/xray-vpn.conf; /etc/init.d/dnsmasq restart; fi; true")


# ---------------------------------------------------------------- utils
def run_cmd(cmd):
    if isinstance(cmd, str) and not IS_WIN:
        cmd = shlex.split(cmd)
    try:
        p = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    except (FileNotFoundError, PermissionError) as e:
        return 1, str(e)
    raw = p.stdout + p.stderr
    for enc in (("oem", "cp866") if IS_WIN else ()) + ("utf-8",):
        try:
            return p.returncode, raw.decode(enc, errors="replace").strip()
        except LookupError:
            continue
    return p.returncode, raw.decode(errors="replace").strip()


def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


ADMIN_HINT = ("Запустите программу от имени администратора." if IS_WIN else
              "Запустите программу с правами root: sudo ./OpenWrtInstaller (нужно для смены IP и TFTP-порта 69).")


# ---------------------------------------------------------------- сеть ноутбука (Windows / Linux / macOS)
def set_static_ip(adapter):
    """IP ноутбука 192.168.1.254/24 на выбранном адаптере. -> (код, вывод)"""
    if IS_WIN:
        return run_cmd(f'netsh interface ip set address name="{adapter}" static {PC_IP} {PC_MASK}')
    if IS_MAC:
        return run_cmd(["networksetup", "-setmanual", adapter, PC_IP, PC_MASK])
    run_cmd(["ip", "link", "set", adapter, "up"])
    code, out = run_cmd(["ip", "addr", "replace", f"{PC_IP}/24", "dev", adapter])
    return code, out


def set_dhcp(adapter):
    if IS_WIN:
        c, o = run_cmd(f'netsh interface ip set address name="{adapter}" source=dhcp')
        run_cmd(f'netsh interface ip set dns name="{adapter}" source=dhcp')
        return c, o
    if IS_MAC:
        return run_cmd(["networksetup", "-setdhcp", adapter])
    # на Linux адрес выдаёт NetworkManager/dhcpcd — просто убираем добавленный статический
    return run_cmd(["ip", "addr", "del", f"{PC_IP}/24", "dev", adapter])


def fw_allow(rule, proto=None, port=None, program=None):
    """Временное правило брандмауэра (Windows: netsh; Linux: ufw, если включён; macOS — не нужно)."""
    if IS_WIN:
        what = f"program=\"{program}\"" if program else f"protocol={proto} localport={port}"
        run_cmd(f'netsh advfirewall firewall add rule name="{rule}" dir=in action=allow {what} profile=any')
    elif not IS_MAC and port and shutil.which("ufw") and "active" in run_cmd(["ufw", "status"])[1].split("\n")[0]:
        run_cmd(["ufw", "allow", f"{port}/{proto.lower()}", "comment", rule])


def fw_remove(rule, proto=None, port=None):
    if IS_WIN:
        run_cmd(f'netsh advfirewall firewall delete rule name="{rule}"')
    elif not IS_MAC and port and shutil.which("ufw"):
        run_cmd(["ufw", "delete", "allow", f"{port}/{proto.lower()}"])


def open_folder(path):
    try:
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", path])
    except Exception:
        pass


def find_files():
    found = {}
    for key, pats in PATTERNS.items():
        found[key] = None
        for d in FW_DIRS:
            for pat in pats:
                m = sorted(glob.glob(os.path.join(d, pat)))
                if m:
                    found[key] = m[0]
                    break
            if found[key]:
                break
    return found


def list_adapters():
    try:
        if IS_WIN:
            cmd = ["powershell", "-NoProfile", "-Command",
                   "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                   "Get-NetAdapter -Physical | ForEach-Object { $_.Name }"]
            p = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
            return [x.strip() for x in p.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
        if IS_MAC:
            # имена «сетевых служб» (Ethernet, USB 10/100/1000 LAN, Wi-Fi) — их понимает networksetup
            out = run_cmd(["networksetup", "-listallnetworkservices"])[1]
            names = [l.strip().lstrip("*") for l in out.splitlines()[1:] if l.strip()]
            return sorted(names, key=lambda n: ("wi-fi" in n.lower() or "bluetooth" in n.lower(), n))
        names = sorted(os.listdir("/sys/class/net"))
        phys = [n for n in names if os.path.exists(f"/sys/class/net/{n}/device")]
        wired = [n for n in phys if not os.path.exists(f"/sys/class/net/{n}/wireless")]
        return wired + [n for n in phys if n not in wired] + [n for n in names if n not in phys and n != "lo"]
    except Exception:
        return []


def ping(ip, timeout_ms=1000):
    if IS_WIN:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    elif IS_MAC:
        cmd = ["ping", "-c", "1", "-W", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    code, out = run_cmd(cmd)
    return code == 0 and "TTL=" in out.upper()


def tcp_open(ip, port, timeout=2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- TFTP
class TftpServer:
    """Минимальный TFTP-сервер (только чтение). Отдаёт один файл на любой запрос."""

    def __init__(self, ip, path, log):
        self.ip, self.path, self.log = ip, path, log
        self.sock = None
        self.stop_flag = threading.Event()
        self.done = threading.Event()

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.ip, 69))
        self.sock.settimeout(1)
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        try:
            self.sock.close()
        except Exception:
            pass

    def _loop(self):
        while not self.stop_flag.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) >= 2 and struct.unpack("!H", data[:2])[0] == 1:
                threading.Thread(target=self._send, args=(data, addr), daemon=True).start()

    def _send(self, req, addr):
        try:
            self._send_inner(req, addr)
        except Exception as e:
            self.log(f"TFTP: ошибка передачи: {e}")

    def _send_inner(self, req, addr):
        parts = req[2:].split(b"\0")
        name = parts[0].decode(errors="replace")
        opts = {parts[i].decode().lower(): parts[i + 1].decode()
                for i in range(2, len(parts) - 1, 2) if parts[i]}
        size = os.path.getsize(self.path)
        self.log(f'TFTP: запрос "{name}" от {addr[0]}, отдаю '
                 f'{os.path.basename(self.path)} ({size} байт)')
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.ip, 0))
        s.settimeout(2)
        blk, oack = 512, b""
        if "blksize" in opts:
            blk = max(8, min(int(opts["blksize"]), 1468))
            oack += b"blksize\0" + str(blk).encode() + b"\0"
        if "tsize" in opts:
            oack += b"tsize\0" + str(size).encode() + b"\0"
        if oack:
            self._xfer(s, addr, b"\x00\x06" + oack, 0)
        sent, last = 0, -1
        n = 1
        with open(self.path, "rb") as f:
            while True:
                chunk = f.read(blk)
                self._xfer(s, addr, struct.pack("!HH", 3, n & 0xFFFF) + chunk, n & 0xFFFF)
                sent += len(chunk)
                pct = sent * 100 // max(size, 1) // 10 * 10
                if pct != last:
                    last = pct
                    self.log(f"TFTP: отправлено {pct}%")
                if len(chunk) < blk:
                    break
                n += 1
        s.close()
        self.log("TFTP: передача завершена")
        self.done.set()

    @staticmethod
    def _xfer(s, addr, packet, expect):
        for _ in range(8):
            s.sendto(packet, addr)
            try:
                while True:
                    d, a = s.recvfrom(1024)
                    if a[0] != addr[0] or len(d) < 4:
                        continue
                    op, b = struct.unpack("!HH", d[:4])
                    if op == 4 and b == expect:
                        return
                    if op == 5:
                        raise RuntimeError("клиент прервал передачу")
            except socket.timeout:
                continue
        raise RuntimeError("таймаут ожидания ACK от роутера")


# ---------------------------------------------------------------- SSH
class Conn:
    def __init__(self, host, user, passwords):
        self.t = None
        try:
            self.t = paramiko.Transport((host, 22))
            self.t.banner_timeout = 20
            self.t.start_client(timeout=20)
            try:
                self.t.auth_none(user)
            except Exception:
                pass
            if not self.t.is_authenticated():
                for pw in passwords:
                    try:
                        self.t.auth_password(user, pw)
                        break
                    except paramiko.AuthenticationException:
                        continue
        except (paramiko.SSHException, OSError, EOFError) as e:
            self.close()
            raise StepError(f"SSH: соединение не удалось ({e}).")
        if not self.t.is_authenticated():
            self.close()
            raise StepError("SSH: неверный логин/пароль (или вход по SSH запрещён).")

    def run(self, cmd, timeout=120):
        ch = self.t.open_session()
        ch.settimeout(timeout)
        ch.set_combine_stderr(True)
        ch.exec_command(cmd)
        out = b""
        while True:
            d = ch.recv(4096)
            if not d:
                break
            out += d
        code = ch.recv_exit_status()
        return code, out.decode(errors="replace").strip()

    def put(self, local, remote, log):
        size = os.path.getsize(local)
        ch = self.t.open_session()
        ch.settimeout(60)
        ch.exec_command(f"cat > '{remote}'")
        sent, last = 0, -1
        with open(local, "rb") as f:
            while True:
                chunk = f.read(32768)
                if not chunk:
                    break
                ch.sendall(chunk)
                sent += len(chunk)
                pct = sent * 100 // max(size, 1) // 25 * 25
                if pct != last:
                    last = pct
                    log(f"  загрузка {os.path.basename(local)}: {pct}%")
        ch.shutdown_write()
        code = ch.recv_exit_status()
        if code != 0:
            raise StepError(f"Не удалось записать {remote} на роутер (код {code}).")

    def put_text(self, text, remote, mode="644"):
        data = text.replace("\r\n", "\n").encode("utf-8")
        ch = self.t.open_session()
        ch.settimeout(60)
        ch.exec_command(f"cat > '{remote}' && chmod {mode} '{remote}'")
        ch.sendall(data)
        ch.shutdown_write()
        if ch.recv_exit_status() != 0:
            raise StepError(f"Не удалось записать {remote} на роутер.")

    def verify(self, local, remote, log):
        code, out = self.run(f"md5sum '{remote}'")
        tok = out.split()[0] if out.split() else ""
        if code == 0 and len(tok) == 32:
            if tok.lower() != md5_of(local):
                raise StepError(f"Контрольная сумма {remote} не совпала — файл повреждён при передаче.")
            log(f"  MD5 совпала ({tok})")
            return
        code, out = self.run(f"wc -c < '{remote}'")
        if code != 0 or out.strip() != str(os.path.getsize(local)):
            raise StepError(f"Размер {remote} на роутере не совпал с локальным файлом.")
        log("  размер файла совпал (md5sum на роутере недоступен)")

    def close(self):
        try:
            if self.t:
                self.t.close()
        except Exception:
            pass


# ---------------------------------------------------------------- main flow
def wait_ping(ip, want_up, timeout, log=None):
    end = time.time() + timeout
    fails = 0
    while time.time() < end:
        ok = ping(ip)
        if want_up and ok:
            return True
        if not want_up:
            fails = 0 if ok else fails + 1
            if fails >= 2:
                return True
        time.sleep(1)
    return False


def connect_retry(router, users, log, tries=12, delay=10):
    """Recovery-образ может поднимать SSH с задержкой: пробуем несколько раз."""
    last = None
    for n in range(1, tries + 1):
        for user, pws in users:
            try:
                return Conn(router, user, pws)
            except StepError as e:
                last = e
        log(f"  SSH ещё не готов (попытка {n}/{tries}), жду {delay} с...")
        time.sleep(delay)
    raise StepError(f"Не удалось войти по SSH в recovery: {last}\n"
                    "Можно прошить вручную: http://192.168.1.1 → System → Backup/Flash Firmware, "
                    "выбрать sysupgrade.itb и снять галку Keep settings.")


def do_sysupgrade(router, files, passwords, log):
    log("Шаг 9/9: подключение к recovery и прошивка sysupgrade")
    conn = connect_retry(router, [("root", [""] + passwords)], log)
    try:
        conn.put(files["sysupgrade"], "/tmp/sysupgrade.itb", log)
        conn.verify(files["sysupgrade"], "/tmp/sysupgrade.itb", log)
        log("  запускаю sysupgrade -n -v (НЕ ОТКЛЮЧАЙТЕ ПИТАНИЕ ~5 минут!)")
        try:
            code, out = conn.run("sysupgrade -n -v /tmp/sysupgrade.itb", timeout=60)
            log("  " + out.replace("\n", "\n  "))
        except Exception:
            log("  соединение закрыто роутером — это нормально, идёт прошивка")
    finally:
        conn.close()
    log("ГОТОВО. Подождите 3-5 минут до перезагрузки, затем откройте http://192.168.1.1 "
        "(логин root, пароль пустой). Потом нажмите «Вернуть DHCP».")


def finish_sysupgrade(cfg, log):
    """Если recovery уже загружен, а sysupgrade не прошёл: просто дошить."""
    files = find_files()
    if not files["sysupgrade"]:
        raise StepError("Не найден файл *squashfs-sysupgrade.itb в папке firmware.")
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not wait_ping(cfg["router"], True, 30):
        raise StepError(f"Роутер {cfg['router']} не отвечает. Recovery загружен? Кабель в LAN, IP ноутбука 192.168.1.254?")
    do_sysupgrade(cfg["router"], files, [cfg["password"]] if cfg["password"] else [], log)


def install(cfg, log):
    adapter, router, user, pw = cfg["adapter"], cfg["router"], cfg["user"], cfg["password"]
    passwords = [pw] if pw else [""]

    # 1. файлы
    log("Шаг 1/9: проверка файлов прошивки")
    files = find_files()
    missing = [k for k, v in files.items() if not v]
    if missing:
        raise StepError("Не найдены файлы прошивки: " + ", ".join(missing) +
                        f"\nПоложите их рядом с программой ({BASE_DIR}) или в подпапку firmware.")
    for k, v in files.items():
        log(f"  {k}: {os.path.basename(v)}")
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not is_admin():
        raise StepError(ADMIN_HINT)
    if not adapter:
        raise StepError("Выберите сетевой адаптер (тот, в который воткнут кабель роутера).")

    # 2. сеть
    log(f"Шаг 2/9: адаптер «{adapter}» -> {PC_IP}/{PC_MASK}")
    code, out = set_static_ip(adapter)
    if code != 0:
        raise StepError(f"Не удалось задать IP {PC_IP} на «{adapter}»: {out}")
    fw_allow(FW_RULE, "UDP", 69)
    fw_allow(FW_RULE, program=sys.executable)
    log("  добавлено временное правило брандмауэра (удалится по завершении)")
    time.sleep(4)

    # 3. TFTP
    log("Шаг 3/9: запуск TFTP-сервера")
    tftp = TftpServer(PC_IP, files["recovery"], log)
    try:
        tftp.start()
    except OSError as e:
        raise StepError(f"Не удалось запустить TFTP на {PC_IP}:69 ({e}). "
                        "Закройте tftpd64/другие TFTP-серверы.")

    conn = None
    try:
        # 4. SSH
        log(f"Шаг 4/9: подключение к {router} по SSH")
        if not wait_ping(router, True, 30):
            raise StepError(f"Роутер {router} не отвечает на ping. Проверьте кабель/LAN-порт/адаптер.")
        conn = Conn(router, user, passwords)
        log("  SSH: вход выполнен")

        # 5. uboot
        log("Шаг 5/9: загрузка uboot.fip в /tmp")
        conn.put(files["fip"], "/tmp/uboot.fip", log)
        conn.verify(files["fip"], "/tmp/uboot.fip", log)

        # 6. mtd
        log("Шаг 6/9: запись загрузчика (НЕ ОТКЛЮЧАЙТЕ ПИТАНИЕ!)")
        code, out = conn.run("mtd -e FIP write /tmp/uboot.fip FIP", timeout=180)
        log("  " + out.replace("\n", "\n  "))
        if code != 0:
            raise StepError(f"mtd write завершился с ошибкой (код {code}).")
        log("Шаг 6/9: очистка раздела ubi")
        code, out = conn.run("mtd erase ubi", timeout=180)
        log("  " + out.replace("\n", "\n  "))
        if code != 0:
            raise StepError(f"mtd erase ubi завершился с ошибкой (код {code}).")

        # 7. reboot
        log("Шаг 7/9: перезагрузка роутера")
        try:
            conn.run("reboot", timeout=5)
        except Exception:
            pass
        conn.close()
        conn = None

        # 8. ожидание recovery
        log("Шаг 8/9: жду перезагрузку (ping 192.168.1.1)...")
        if not wait_ping(router, False, 90):
            log("  ВНИМАНИЕ: роутер не ушёл в перезагрузку, продолжаю ждать TFTP")
        log("  жду, пока U-Boot скачает recovery.itb по TFTP (до 5 мин)...")
        if not tftp.done.wait(300):
            raise StepError(
                "Роутер не скачал recovery.itb за 5 минут.\n"
                "Проверьте кабель в LAN-порт и IP 192.168.1.254 на ПК. "
                "Если не помогает — понадобится UART/ручное восстановление.")
        log("  recovery передан, жду загрузку системы и SSH (до 5 мин)...")
        end = time.time() + 300
        while time.time() < end and not tcp_open(router, 22):
            time.sleep(3)
        if not tcp_open(router, 22):
            raise StepError("Recovery-образ не поднял SSH за 5 минут.")
        time.sleep(10)

        # 9. sysupgrade
        do_sysupgrade(router, files, passwords, log)
    finally:
        if conn:
            conn.close()
        tftp.stop()
        fw_remove(FW_RULE, "UDP", 69)


# ---------------------------------------------------------------- VPN: установка
def _tail(out, n=12):
    lines = [l for l in out.splitlines() if l.strip()]
    return "\n".join("  " + l for l in lines[-n:])


def _xray_ready(conn):
    """Уже стоит xray и geo-файлы? Возвращает папку geo-файлов или None."""
    if conn.run("[ -x /usr/bin/xray ]")[0] != 0:
        return None
    for d in (conn.run("cat /etc/xray-vpn/asset-dir 2>/dev/null")[1].strip(), "/usr/share/v2ray", "/etc/xray-vpn/geo"):
        if d and conn.run(f"[ -s {d}/geosite.dat ] && [ -s {d}/geoip.dat ]")[0] == 0:
            return d
    return None


def _xray_setup_packages(conn, log):
    """Ставит xray-core и geoip/geosite. Возвращает папку geo-файлов."""
    code, out = conn.run("ping -c 2 -W 3 1.1.1.1")
    if code != 0:
        raise StepError("У роутера нет интернета. Подключите кабель провайдера в порт WAN "
                        "и повторите (пакеты ставятся из интернета).")
    pm = "apk" if conn.run("command -v apk")[0] == 0 else "opkg"
    log(f"  менеджер пакетов: {pm}")
    code, out = conn.run("df -k /overlay | tail -n 1 | awk '{print $4}'")
    if out.strip().isdigit() and int(out.strip()) < 60000:
        raise StepError(f"На роутере мало места для xray и geo-файлов (свободно {int(out.strip()) // 1024} МБ, "
                        "нужно около 60 МБ).")
    code, out = conn.run("grep MemAvailable /proc/meminfo | awk '{print int($2/1024)}'")
    if out.strip().isdigit() and int(out.strip()) < 150:
        log(f"  ВНИМАНИЕ: свободной памяти мало ({out.strip()} МБ), xray с geosite может не влезть")
    log("  установка xray-core (несколько минут)")
    steps = ["apk update", "apk add xray-core ca-bundle"] if pm == "apk" else \
            ["opkg update", "opkg install xray-core ca-bundle"]
    for s in steps:
        code, out = conn.run(s, timeout=600)
        log(f"  $ {s}\n{_tail(out)}")
        if code != 0:
            raise StepError(f"Команда «{s}» завершилась с ошибкой. Пакета xray-core нет в репозитории этой прошивки?")
    log("  установка geoip/geosite")
    pk = "apk add v2ray-geoip v2ray-geosite" if pm == "apk" else "opkg install v2ray-geoip v2ray-geosite"
    code, out = conn.run(pk, timeout=600)
    log(_tail(out, 6))
    if conn.run("[ -s /usr/share/v2ray/geosite.dat ] && [ -s /usr/share/v2ray/geoip.dat ]")[0] == 0:
        return "/usr/share/v2ray"
    log("  пакетов geoip/geosite нет, скачиваю с GitHub (v2fly)")
    conn.run("mkdir -p /etc/xray-vpn/geo")
    for url, name in (("https://github.com/v2fly/geoip/releases/latest/download/geoip.dat", "geoip.dat"),
                      ("https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat",
                       "geosite.dat")):
        code, out = conn.run(f"uclient-fetch -q -O /etc/xray-vpn/geo/{name} '{url}' || "
                             f"wget -q -O /etc/xray-vpn/geo/{name} '{url}'", timeout=300)
        if code != 0 or conn.run(f"[ -s /etc/xray-vpn/geo/{name} ]")[0] != 0:
            raise StepError(f"Не удалось скачать {name}. Проверьте доступ роутера к GitHub.")
    return "/etc/xray-vpn/geo"


def router_session(cfg, log, title):
    """Пинг + SSH к роутеру. Возвращает Conn."""
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    router = cfg["router"]
    log(f"{title}: подключение к {router}")
    if not wait_ping(router, True, 20):
        raise StepError(f"Роутер {router} не отвечает. Он включён? Кабель в LAN-порт?")
    return connect_any(router, cfg["user"], cfg["password"])


def _read_current_outbounds(conn):
    """Текущий ключ на роутере (новый формат или config.json старой версии) либо None."""
    for path in (f"{XD}/outbound.json", f"{XD}/config.json"):
        code, out = conn.run(f"cat {path} 2>/dev/null")
        try:
            obs = json.loads(out)["outbounds"]
            obs[0]["settings"]["vnext"][0]["address"]
            for ob in obs:
                if ob.get("tag") == "direct":
                    ob["settings"] = {"domainStrategy": "UseIPv4"}
            return {"outbounds": obs}
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return None


def vpn_install(cfg, log, conn=None):
    """Настраивает VPN. Если xray уже стоит — ничего не переустанавливает: меняет только ключ
    (если вставлен новый), список серверов и маршрутизацию.
    cfg: vless — выбранный ключ (может быть пуст — тогда ключ с роутера);
         servers — все ключи подписки для автопереключения (необязательно);
         zapret_mode — YouTube/Discord сначала через zapret; user_direct / user_proxy — свои исключения."""
    zapret_mode = bool(cfg.get("zapret_mode"))
    link = cfg.get("vless", "").strip()
    servers = [s for s in (cfg.get("servers") or []) if s.strip()]
    if link and link not in servers:
        servers = [link]
    excluded = set(cfg.get("excluded") or [])
    if excluded:
        servers = [s for s in servers if s == link or link_name(s) not in excluded]
    for s in servers:
        build_xray_config(s)                     # проверить все ключи до изменений на роутере
    own = conn is None
    if own:
        conn = router_session(cfg, log, "VPN 1/4")
    try:
        if forkop_installed(conn) and conn.run("[ -e /etc/rc.d/S99forkop ] || pidof sing-box >/dev/null")[0] == 0:
            log("  Forkop включён — выключаю его, чтобы работал встроенный VPN")
            forkop_disable(cfg, log, conn)
        cur_out = None
        if not link:
            cur_out = _read_current_outbounds(conn)
            if cur_out is None:
                raise StepError("Нет vless-ссылки: вставьте её в разделе «Обход блокировок» или выберите сервер из подписки.")
            log("  ссылка не вставлена — использую ключ, уже сохранённый на роутере")

        asset_dir = _xray_ready(conn)
        if asset_dir:
            log("VPN 2/4: xray уже установлен — ничего не переустанавливаю, меняю только настройки")
        else:
            log("VPN 2/4: установка xray-core и geo-файлов")
            asset_dir = _xray_setup_packages(conn, log)
        log(f"  geo-файлы: {asset_dir}")
        if conn.run("command -v curl >/dev/null")[0] != 0:
            pm = "apk add" if conn.run("command -v apk")[0] == 0 else "opkg install"
            conn.run(f"{pm} curl", timeout=300)
            if conn.run("command -v curl >/dev/null")[0] != 0:
                log("  ВНИМАНИЕ: curl не установился — сторож (автопереключение) работать не будет")
        conn.run("[ -x /etc/init.d/xray ] && { /etc/init.d/xray stop; /etc/init.d/xray disable; }; true")

        log("VPN 3/4: запись настроек — " + ("YouTube и Discord сначала через zapret, " if zapret_mode else "")
            + "РФ, Google, Apple, Microsoft и торренты напрямую, остальное через VPN")
        conn.run(f"mkdir -p {XD}")
        conn.run(OLD_LIST_CLEANUP, timeout=60)
        conn.put_text(json.dumps(build_base_config(), indent=1), f"{XD}/base.json", "600")
        if link:
            conn.run(f"rm -rf {XD}/servers; mkdir -p {XD}/servers")
            cur_idx = 1
            for i, s in enumerate(servers, 1):
                conn.put_text(json.dumps(build_outbounds(s), indent=1), f"{XD}/servers/{i:02d}.json", "600")
                conn.put_text(link_name(s), f"{XD}/servers/{i:02d}.name")
                if s == link:
                    cur_idx = i
            conn.run(f"cp {XD}/servers/{cur_idx:02d}.json {XD}/outbound.json; echo {cur_idx:02d} > {XD}/current")
            log(f"  серверов сохранено на роутере: {len(servers)}; текущий: {link_name(link)}")
        else:
            conn.put_text(json.dumps(cur_out, indent=1), f"{XD}/outbound.json", "600")
        conn.run(f"rm -f {XD}/config.json")

        env = f"XRAY_LOCATION_ASSET={asset_dir} "
        ud, up = cfg.get("user_direct", ""), cfg.get("user_proxy", "")
        variants = [("routing-main.json", False)] + ([("routing-ytvpn.json", True)] if zapret_mode else [])
        for sites in (DIRECT_GEOSITES, []):
            for fname, ytvpn in variants:
                conn.put_text(json.dumps(build_routing(zapret_mode, sites, ytvpn, ud, up), indent=1),
                              f"{XD}/{fname}")
            conn.run(f"cp {XD}/routing-main.json {XD}/routing.json")
            code, out = conn.run(env + f"/usr/bin/xray run -test {XRAY_FILES}", timeout=180)
            if code == 0 and zapret_mode:
                code, out = conn.run(env + f"/usr/bin/xray run -test -c {XD}/base.json -c {XD}/outbound.json "
                                     f"-c {XD}/routing-ytvpn.json", timeout=180)
            if code == 0:
                break
            if sites and re.search(r"geosite", out, re.I) and not re.search(r"category-ru", out, re.I):
                log("  в geosite нет списков google/apple/microsoft — они пойдут по общему правилу (geoip:ru)")
                continue
            raise StepError("xray не принял настройки:\n" + _tail(out))
        if not zapret_mode:
            conn.run(f"rm -f {XD}/routing-ytvpn.json")
        conn.run(f"echo zapret > {XD}/yt_state; rm -f /tmp/xwd_*")
        log("  настройки xray проверены")
        failover = 1 if conn.run(f"[ $(ls {XD}/servers/*.json 2>/dev/null | wc -l) -gt 1 ]")[0] == 0 else 0
        conn.put_text(f"FAILOVER={failover}\nZAPRET_MODE={1 if zapret_mode else 0}\n"
                      f"LAT_SWITCH={1 if cfg.get('lat_switch', True) else 0}\nLAT_DIFF=80\nE2E_MAX=3000\n",
                      f"{XD}/watchdog.conf")
        conn.put_text(WATCHDOG_SH, f"{XD}/watchdog.sh", "755")
        code, hosts_out = conn.run(f"cat {XD}/servers/*.json {XD}/outbound.json 2>/dev/null")
        hosts = set()
        for m_ in re.finditer(r'"address":\s*"([^"]+)"', hosts_out):
            h = m_.group(1)
            if re.fullmatch(r"[A-Za-z0-9.-]+", h) and not re.fullmatch(r"[\d.]+", h):
                hosts.add(h.lower())
        conn.put_text("\n".join(sorted(hosts)) + "\n", f"{XD}/dns-hosts")
        conn.run(f"rm -f {XD}/dns-vpn.sh")
        ensure_dnsctl(conn)
        mode_now = conn.run(f"sed -n 's/^DNS_MODE=//p' {DNSCTL}/dns.conf")[1].strip() or "auto"
        log(f"  DNS: {DNS_MODES.get(mode_now, mode_now)} (меняется в разделе «Интернет, Wi-Fi, DNS»)")
        conn.put_text(GEO_UPDATE_SH, f"{XD}/geo-update.sh", "755")
        conn.put_text(SUB_UPDATE_UC, f"{XD}/sub-update.uc", "755")
        conn.put_text("".join(n + "\n" for n in sorted(excluded)), f"{XD}/exclude.names")
        if excluded:
            log(f"  исключено серверов: {len(excluded)} — роутер их не использует")
        sub_url = (cfg.get("sub_url") or "").strip()
        if sub_url and link:
            conn.put_text(sub_url + "\n", f"{XD}/sub.url", "600")
            log("  подписка: роутер сам обновляет её каждые 6 часов (новые ключи и серверы подхватятся без ноутбука)"
                + ("" if conn.run("command -v ucode >/dev/null")[0] == 0 else
                   " — ВНИМАНИЕ: на роутере нет ucode, автообновление работать не будет"))
        elif link:
            conn.run(f"rm -f {XD}/sub.url {XD}/sub.updated")
        conn.put_text(build_nft_rules_all(), f"{XD}/rules.nft")
        conn.put_text(asset_dir, f"{XD}/asset-dir")
        conn.put_text(INIT_SCRIPT, "/etc/init.d/xray-vpn", "755")
        conn.run(CRON_SETUP, timeout=60)
        log("  сторож: " + ("автопереключение серверов вкл., " if failover else "")
            + ("уход с медленного сервера на быстрый вкл., " if failover and cfg.get("lat_switch", True) else "")
            + ("YouTube: zapret → VPN при сбое, " if zapret_mode else "") + "проверка каждые 3 минуты")

        log("VPN 4/4: запуск")
        conn.run("/etc/init.d/xray-vpn enable && /etc/init.d/xray-vpn restart", timeout=60)
        time.sleep(4)
        if conn.run("pidof xray >/dev/null")[0] != 0:
            raise StepError("xray не запустился. Смотрите: logread | grep xray")
        if conn.run("nft list table inet xray >/dev/null 2>&1")[0] != 0:
            raise StepError("Правила nftables не загрузились (проверьте, что LAN-мост называется br-lan).")
        code, out = conn.run(f"curl -s -o /dev/null -m 12 -w '%{{http_code}}' -x socks5h://127.0.0.1:{TEST_SOCKS_PORT} "
                             "https://www.gstatic.com/generate_204", timeout=30)
        if out.strip() == "204":
            log("  сервер VPN отвечает ✔")
        else:
            log(f"  ВНИМАНИЕ: сервер VPN не ответил на проверку (код {out.strip() or '—'}). "
                "Проверьте ключ или выберите другой сервер.")
        log("  xray запущен. Не работает — «Выключить VPN» вернёт прямой интернет.")
    finally:
        if own:
            conn.close()


def vpn_diag(cfg, log):
    conn = router_session(cfg, log, "Диагностика VPN")
    try:
        def show(title, cmd, tail=8):
            code, out = conn.run(cmd, timeout=60)
            log(f"  [{title}] " + ("OK" if code == 0 else f"код {code}"))
            if out.strip():
                log(_tail(out, tail))
        show("xray запущен", "pidof xray")
        show("версия xray", "xray version | head -n 1", 1)
        show("текущий сервер", f"cat {XD}/servers/$(cat {XD}/current).name", 1)
        show("geo-файлы", f"ls -la $(cat {XD}/asset-dir)", 4)
        show("zapret запущен", "pidof nfqws", 1)
        show("YouTube сейчас идёт через", f"cat {XD}/yt_state", 1)
        show("правила nft", "nft list chain inet xray prerouting", 12)
        show("сервер VPN (через xray)", f"curl -s -o /dev/null -m 10 -w '%{{http_code}}' "
             f"-x socks5h://127.0.0.1:{TEST_SOCKS_PORT} https://www.gstatic.com/generate_204", 1)
        show("YouTube напрямую (zapret)", "curl -s -o /dev/null -m 10 -w '%{http_code}' "
             "https://www.youtube.com/generate_204", 1)
        show("журнал сторожа", "logread -e xray-watchdog | tail -n 6", 6)
        show("ошибки xray", "logread | grep -i 'xray\\[' | tail -n 8", 8)
        log("Диагностика завершена. Скопируйте это из окна журнала и пришлите.")
    finally:
        conn.close()


def vpn_rollback(cfg, log, conn=None):
    own = conn is None
    if own:
        conn = router_session(cfg, log, "Выключение VPN")
    try:
        conn.run("[ -x /etc/init.d/xray-vpn ] && { /etc/init.d/xray-vpn stop; /etc/init.d/xray-vpn disable; }; "
                 "nft delete table inet xray 2>/dev/null; true", timeout=60)
        conn.run(OLD_LIST_CLEANUP, timeout=60)
        log("VPN выключен: трафик идёт напрямую (xray и ключи остались на роутере, включить можно без переустановки).")
    finally:
        if own:
            conn.close()


def vpn_forget(cfg, log):
    """Удаляет с роутера все сохранённые ключи (серверы) и выключает VPN."""
    conn = router_session(cfg, log, "Удаление ключей с роутера")
    try:
        vpn_rollback(cfg, log, conn)
        conn.run(f"rm -rf {XD}/servers {XD}/outbound.json {XD}/current {XD}/config.json {XD}/sub.url "
                 f"{XD}/sub.updated /tmp/xwd_*; true")
        log("  ключи и серверы подписки удалены с роутера")
    finally:
        conn.close()


def sub_update_now(cfg, log):
    conn = router_session(cfg, log, "Обновление подписки на роутере")
    try:
        if conn.run(f"[ -s {XD}/sub.url ]")[0] != 0:
            raise StepError("На роутере не сохранена подписка. Загрузите серверы из подписки, выберите сервер "
                            "и нажмите «Применить» в «Обходе блокировок» (галочка автообновления должна стоять).")
        conn.put_text(SUB_UPDATE_UC, f"{XD}/sub-update.uc", "755")
        conn.put_text("".join(n + "\n" for n in sorted(set(cfg.get("excluded") or []))), f"{XD}/exclude.names")
        code, out = conn.run(f"/usr/bin/ucode {XD}/sub-update.uc 2>&1", timeout=180)
        log(_tail(out, 4))
        if code != 0:
            raise StepError("Подписка не обновилась:\n" + _tail(out, 3))
    finally:
        conn.close()


def geo_update(cfg, log):
    conn = router_session(cfg, log, "Обновление geo-баз")
    try:
        if conn.run("[ -x /usr/bin/xray ]")[0] != 0:
            raise StepError("xray не установлен — сначала настройте VPN.")
        conn.run(f"mkdir -p {XD}")
        conn.put_text(GEO_UPDATE_SH, f"{XD}/geo-update.sh", "755")
        log("  скачиваю свежие базы с GitHub на роутер (1–3 минуты)")
        code, out = conn.run(f"sh {XD}/geo-update.sh", timeout=900)
        log(_tail(out, 4))
        if code != 0:
            raise StepError("Базы не обновлены:\n" + _tail(out, 3))
    finally:
        conn.close()


def server_latency(cfg, log):
    """Замер задержки (TCP-подключение) с роутера до каждого сервера. -> {индекс: мс или None}"""
    items = cfg["servers_list"]
    conn = router_session(cfg, log, "Проверка задержки")
    try:
        parts = []
        for i, (_, addr, _, _) in enumerate(items):
            host, _, port = addr.rpartition(":")
            if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host) or not port.isdigit():
                continue
            h = f"[{host}]" if ":" in host else host
            parts.append(f"( t=$(curl -s -o /dev/null -m 4 -w '%{{time_connect}}' 'http://{h}:{port}/' 2>/dev/null); "
                         f"echo \"{i} ${{t:-0}}\" ) &")
        if conn.run("command -v curl >/dev/null")[0] != 0:
            conn.run("apk add curl 2>/dev/null || opkg install curl", timeout=300)
        code, out = conn.run("\n".join(parts) + "\nwait", timeout=60)
    finally:
        conn.close()
    res = {}
    for line in out.splitlines():
        m = re.fullmatch(r"(\d+) ([\d.,]+)", line.strip())
        if m:
            t = float(m.group(2).replace(",", "."))
            res[int(m.group(1))] = int(t * 1000) if t > 0 else None
    log(f"  отвечают {sum(1 for v in res.values() if v)} из {len(items)}")
    return res


def router_status(cfg, log):
    """Сводка состояния роутера для вкладки «Состояние». -> [(название, текст, ok: True/False/None)]"""
    conn = router_session(cfg, log, "Состояние")
    rows = []
    try:
        def get(cmd, t=30):
            return conn.run(cmd, timeout=t)[1].strip()
        rows.append(("Прошивка", get(". /etc/openwrt_release; echo $DISTRIB_DESCRIPTION") or "—", None))
        up = get("cut -d. -f1 /proc/uptime")
        if up.isdigit():
            u = int(up)
            rows.append(("Работает", f"{u // 86400} дн {u % 86400 // 3600} ч {u % 3600 // 60} мин", None))
        wan = get("ubus call network.interface.wan status 2>/dev/null | jsonfilter -e '@[\"ipv4-address\"][0].address'")
        net = conn.run("ping -c 1 -W 3 1.1.1.1 >/dev/null")[0] == 0
        rows.append(("Интернет (WAN)", ("есть" if net else "нет") + (f", IP {wan}" if wan else ""), net))
        xr_on = conn.run("[ -e /etc/rc.d/S99xray-vpn ] && pidof xray >/dev/null")[0] == 0
        zp_on = conn.run("pidof nfqws >/dev/null")[0] == 0
        fk_on_ = conn.run("[ -e /etc/rc.d/S99forkop ] && pidof sing-box >/dev/null")[0] == 0
        vpn_name = "встроенный VPN" if xr_on else "Forkop" if fk_on_ else ""
        mode = (f"{vpn_name} + zapret" if vpn_name and zp_on else vpn_name if vpn_name
                else "только zapret" if zp_on else "обход выключен")
        xr_on_or_fk = xr_on or fk_on_
        rows.append(("Обход блокировок", mode, xr_on_or_fk or zp_on))
        if xr_on:
            name = get(f"cat {XD}/servers/$(cat {XD}/current 2>/dev/null).name 2>/dev/null") or "—"
            n = get(f"ls {XD}/servers/*.json 2>/dev/null | wc -l")
            c = get(f"curl -s -o /dev/null -m 10 -w '%{{http_code}}' -x socks5h://127.0.0.1:{TEST_SOCKS_PORT} "
                    "https://www.gstatic.com/generate_204")
            rows.append(("Сервер VPN", f"{name} (серверов на роутере: {n or 1})"
                         + (" — отвечает" if c == "204" else " — НЕ отвечает"), c == "204"))
            c = get(f"curl -s -o /dev/null -m 10 -w '%{{http_code}}' -x socks5h://127.0.0.1:{TEST_SOCKS_PORT} "
                    "https://api.telegram.org")
            su = get(f"[ -s {XD}/sub.url ] && cat {XD}/sub.updated 2>/dev/null || echo none")
            if su != "none":
                d_, _, n_ = su.partition("|")
                rows.append(("Подписка", ("обновляется автоматически, последний раз " + d_ + f" ({n_} серв.)")
                             if d_ else "обновляется автоматически (ещё не обновлялась)", True))
            lt = get(f"cat {XD}/latency.last 2>/dev/null")
            if lt:
                rows.append(("Задержка", lt, None))
            rows.append(("Telegram (через VPN)", "доступен" if c[:1] in "234" and c != "000" else "недоступен",
                         c[:1] in "234" and c != "000"))
        else:
            rows.append(("Сервер VPN", "VPN выключен", None))
        if zp_on or xr_on:
            st = get(f"cat {XD}/yt_state 2>/dev/null") if xr_on and zp_on else ("zapret" if zp_on else "vpn")
            c = get("curl -s -o /dev/null -m 10 -w '%{http_code}' https://www.youtube.com/generate_204")
            via = "через zapret" if st != "vpn" else "через VPN (zapret не справился, сторож переключил)"
            if not zp_on:
                c = get(f"curl -s -o /dev/null -m 10 -w '%{{http_code}}' -x socks5h://127.0.0.1:{TEST_SOCKS_PORT} "
                        "https://www.youtube.com/generate_204")
            rows.append(("YouTube", via + (" — открывается" if c == "204" else " — не открывается"), c == "204"))
        if conn.run("[ -x /usr/bin/forkop ]")[0] == 0:
            fk_on = conn.run("[ -e /etc/rc.d/S99forkop ] && pidof sing-box >/dev/null")[0] == 0
            ver = get("forkop show_version 2>/dev/null | head -n 1")
            rows.append(("Forkop", ("работает" if fk_on else "установлен, выключен") + (f" ({ver})" if ver else ""),
                         True if fk_on else None))
        eff = get(f"cat {DNSCTL}/state 2>/dev/null") or "isp"
        rows.append(("DNS", DNS_STATE_TEXT.get(eff, eff) + ("" if eff != "isp" else " — провайдер может подменять"),
                     eff != "isp"))
        rows.append(("Zapret", "работает" if zp_on else
                     ("установлен, выключен" if conn.run("[ -x /etc/init.d/zapret ]")[0] == 0 else "не установлен"),
                     True if zp_on else None))
        mem = get("awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo")
        fl = get("df -k /overlay | awk 'NR==2{print int($4/1024)}'")
        rows.append(("Свободно", f"память {mem or '?'} МБ, флеш {fl or '?'} МБ", None))
        lang = get("uci -q get luci.main.lang")
        rows.append(("Язык панели", {"ru": "русский"}.get(lang, lang or "авто/английский"), None))
        pw_empty = conn.run("grep -q '^root::' /etc/shadow")[0] == 0
        rows.append(("Пароль root", "НЕ задан — задайте в разделе «Роутер и компоненты»" if pw_empty else "задан", not pw_empty))
        for name, text, ok in rows:
            log(f"  {name}: {text}")
        return rows
    finally:
        conn.close()


def set_root_password(cfg, log):
    pw = cfg["new_pw"]
    if len(pw) < 6:
        raise StepError("Пароль слишком короткий (минимум 6 символов).")
    if "\n" in pw:
        raise StepError("Недопустимый символ в пароле.")
    conn = router_session(cfg, log, "Смена пароля root")
    try:
        code, out = conn.run(f"printf '%s\\n%s\\n' {shq(pw)} {shq(pw)} | passwd root 2>&1")
        if code != 0:
            raise StepError("passwd вернул ошибку:\n" + _tail(out, 3))
        log("  пароль root изменён (он же — для входа в веб-панель LuCI)")
    finally:
        conn.close()


def luci_russian(cfg, log):
    conn = router_session(cfg, log, "Русский язык")
    try:
        if conn.run("ping -c 2 -W 3 1.1.1.1")[0] != 0:
            raise StepError("У роутера нет интернета (пакеты перевода ставятся из интернета).")
        apk = conn.run("command -v apk")[0] == 0
        add = "apk add" if apk else "opkg install"
        log("  обновление списка пакетов")
        conn.run("apk update" if apk else "opkg update", timeout=300)
        code, out = conn.run(f"{add} luci-i18n-base-ru", timeout=300)
        if code != 0:
            raise StepError("Не удалось установить luci-i18n-base-ru:\n" + _tail(out, 4))
        code, out = conn.run(
            "for f in /usr/share/luci/menu.d/luci-app-*.json; do [ -f \"$f\" ] || continue; "
            "n=$(basename \"$f\" .json); n=${n#luci-app-}; "
            f"{add} luci-i18n-$n-ru >/dev/null 2>&1 && echo \"  + $n\"; done; "
            f"{add} luci-i18n-firewall-ru luci-i18n-package-manager-ru >/dev/null 2>&1; "
            "uci set luci.main.lang=ru; uci commit luci; rm -rf /tmp/luci-*; true", timeout=900)
        if out.strip():
            log(out)
        log("  готово: обновите страницу панели (Ctrl+F5) — она будет на русском")
    finally:
        conn.close()


def factory_reset(cfg, log):
    conn = router_session(cfg, log, "Сброс OpenWrt")
    try:
        code, out = conn.run("command -v firstboot")
        if code != 0:
            raise StepError("На роутере нет firstboot — это точно OpenWrt?")
        conn.run("firstboot -y >/dev/null 2>&1; (sleep 2; reboot) >/dev/null 2>&1 &", timeout=60)
        log("  настройки стёрты, роутер перезагружается (2–3 минуты).")
        log("  После загрузки: 192.168.1.1, логин root без пароля, Wi-Fi выключен, VPN и zapret удалены.")
    finally:
        conn.close()


# ---------------------------------------------------------------- DNS: управление
# Режимы: auto — через VPN, если он работает, иначе зашифрованный DNS (DoH);
#         vpn  — через VPN-туннель; doh — зашифрованный DNS напрямую (подходит для zapret без VPN);
#         isp  — DNS провайдера (как было).
DNS_MODES = {
    "auto": "Авто: через VPN, если он включён, иначе зашифрованный DNS",
    "doh": "Зашифрованный DNS (DoH) напрямую — для zapret, VPN не нужен",
    "vpn": "Через VPN-туннель",
    "isp": "DNS провайдера (как было, провайдер может подменять)",
}
DOH_SERVERS = {
    "Cloudflare": ("https://cloudflare-dns.com/dns-query", "1.1.1.1,1.0.0.1"),
    "Google": ("https://dns.google/dns-query", "8.8.8.8,8.8.4.4"),
    "Quad9": ("https://dns.quad9.net/dns-query", "9.9.9.9,149.112.112.112"),
    "AdGuard (без рекламы)": ("https://dns.adguard-dns.com/dns-query", "94.140.14.14,94.140.15.15"),
}
DNSCTL = "/etc/dnsctl"
DOH_PORT = 5053

DNSCTL_SH = r"""#!/bin/sh
# dnsctl apply [vpn-on|vpn-off] — выставляет DNS роутера по режиму из /etc/dnsctl/dns.conf
C=/etc/dnsctl
DNS_MODE=auto
[ -f $C/dns.conf ] && . $C/dns.conf
case "$2" in
    vpn-on) VPN=1 ;;
    vpn-off) VPN=0 ;;
    *) VPN=0; [ -e /etc/rc.d/S99xray-vpn ] && pidof xray >/dev/null && VPN=1 ;;
esac
DOH=0
[ -x /etc/init.d/https-dns-proxy ] && DOH=1
S=dhcp.@dnsmasq[0]
for v in '127.0.0.1#5353' '127.0.0.1#5053' '1.1.1.1' '8.8.8.8'; do uci -q del_list $S.server="$v"; done
for f in $C/hosts.applied /etc/xray-vpn/dns-hosts.applied; do
    for h in $(cat $f 2>/dev/null); do
        uci -q del_list $S.server="/$h/77.88.8.8"
        uci -q del_list $S.server="/$h/8.8.8.8"
    done
    rm -f $f
done
eff=isp
case "$DNS_MODE" in
    vpn) if [ $VPN = 1 ]; then eff=vpn; elif [ $DOH = 1 ]; then eff=doh; fi ;;
    doh) [ $DOH = 1 ] && eff=doh ;;
    auto) if [ $VPN = 1 ]; then eff=vpn; elif [ $DOH = 1 ]; then eff=doh; fi ;;
esac
if [ $DOH = 1 ]; then
    if [ $eff = isp ]; then
        /etc/init.d/https-dns-proxy stop >/dev/null 2>&1; /etc/init.d/https-dns-proxy disable >/dev/null 2>&1
    else
        /etc/init.d/https-dns-proxy enable >/dev/null 2>&1; /etc/init.d/https-dns-proxy start >/dev/null 2>&1
    fi
fi
case $eff in
    vpn)
        for h in $(cat /etc/xray-vpn/dns-hosts 2>/dev/null); do
            uci add_list $S.server="/$h/77.88.8.8"
            uci add_list $S.server="/$h/8.8.8.8"
        done
        cp /etc/xray-vpn/dns-hosts $C/hosts.applied 2>/dev/null
        uci add_list $S.server='127.0.0.1#5353'
        if [ $DOH = 1 ]; then uci add_list $S.server='127.0.0.1#5053'; else uci add_list $S.server='1.1.1.1'; fi
        ;;
    doh)
        uci add_list $S.server='127.0.0.1#5053'
        ;;
esac
if [ $eff = isp ]; then
    uci -q delete $S.noresolv; uci -q delete $S.strictorder
else
    uci set $S.noresolv='1'; uci set $S.strictorder='1'
fi
if [ $VPN = 1 ]; then uci set $S.filter_aaaa='1'; else uci -q delete $S.filter_aaaa; fi
uci commit dhcp
# устройства с «зашитым» DNS (8.8.8.8 и т.п.) тоже заворачиваем на роутер
nft delete table inet dnsctl 2>/dev/null
if [ $eff != isp ]; then
    nft -f - <<EOF
table inet dnsctl {
    chain prerouting {
        type nat hook prerouting priority dstnat - 5; policy accept;
        iifname "br-lan" udp dport 53 redirect to :53
        iifname "br-lan" tcp dport 53 redirect to :53
    }
}
EOF
fi
echo $eff > $C/state
( sleep 1; /etc/init.d/dnsmasq restart ) >/dev/null 2>&1 &
exit 0
"""

DNSCTL_INIT = """#!/bin/sh /etc/rc.common
START=98
boot() {
    ( sleep 15; sh /etc/dnsctl/dnsctl.sh apply ) >/dev/null 2>&1 &
}
start() {
    sh /etc/dnsctl/dnsctl.sh apply
}
"""

DNS_STATE_TEXT = {"vpn": "через VPN", "doh": "зашифрованный (DoH)", "isp": "провайдера"}


def ensure_dnsctl(conn, mode=None):
    """Кладёт на роутер скрипт управления DNS (не меняя выбранный режим, если mode=None)."""
    conn.run(f"mkdir -p {DNSCTL}")
    conn.put_text(DNSCTL_SH, f"{DNSCTL}/dnsctl.sh", "755")
    conn.put_text(DNSCTL_INIT, "/etc/init.d/dnsctl", "755")
    conn.run("/etc/init.d/dnsctl enable")
    if mode:
        conn.put_text(f"DNS_MODE={mode}\n", f"{DNSCTL}/dns.conf")
    else:
        conn.run(f"[ -f {DNSCTL}/dns.conf ] || echo DNS_MODE=auto > {DNSCTL}/dns.conf")


def _doh_setup(conn, server, log):
    """Ставит и настраивает https-dns-proxy (DoH на 127.0.0.1:5053), не давая ему трогать dnsmasq."""
    url, boot = DOH_SERVERS.get(server, DOH_SERVERS["Cloudflare"])
    if conn.run("[ -x /etc/init.d/https-dns-proxy ]")[0] != 0:
        if conn.run("ping -c 2 -W 3 1.1.1.1")[0] != 0:
            raise StepError("Для зашифрованного DNS нужен пакет https-dns-proxy, а у роутера нет интернета.")
        log("  установка https-dns-proxy (зашифрованный DNS)")
        apk = conn.run("command -v apk")[0] == 0
        conn.run("apk update" if apk else "opkg update", timeout=300)
        code, out = conn.run("apk add https-dns-proxy" if apk else "opkg install https-dns-proxy", timeout=300)
        if conn.run("[ -x /etc/init.d/https-dns-proxy ]")[0] != 0:
            raise StepError("Не удалось установить https-dns-proxy:\n" + _tail(out, 4))
    conn.run("uci -q get https-dns-proxy.config >/dev/null || uci set https-dns-proxy.config=main; "
             "uci set https-dns-proxy.config.dnsmasq_config_update='-'; "
             "uci set https-dns-proxy.config.force_dns='0'; "
             "while uci -q delete https-dns-proxy.@https-dns-proxy[0]; do :; done; "
             "uci add https-dns-proxy https-dns-proxy >/dev/null; "
             f"uci set https-dns-proxy.@https-dns-proxy[-1].resolver_url='{url}'; "
             f"uci set https-dns-proxy.@https-dns-proxy[-1].bootstrap_dns='{boot}'; "
             "uci set https-dns-proxy.@https-dns-proxy[-1].listen_addr='127.0.0.1'; "
             f"uci set https-dns-proxy.@https-dns-proxy[-1].listen_port='{DOH_PORT}'; "
             "uci commit https-dns-proxy; /etc/init.d/https-dns-proxy restart", timeout=60)
    log(f"  зашифрованный DNS: {server} ({url})")


def dns_apply(cfg, log, conn=None):
    """Применяет режим DNS cfg['dns_mode'] с DoH-сервером cfg['doh']."""
    mode = cfg.get("dns_mode") or "auto"
    own = conn is None
    if own:
        conn = router_session(cfg, log, "DNS")
    try:
        log(f"DNS: режим «{DNS_MODES.get(mode, mode)}»")
        if mode != "isp":
            _doh_setup(conn, cfg.get("doh") or "Cloudflare", log)
        ensure_dnsctl(conn, mode)
        conn.run(f"sh {DNSCTL}/dnsctl.sh apply", timeout=60)
        time.sleep(3)
        eff = conn.run(f"cat {DNSCTL}/state")[1].strip()
        ok = conn.run("nslookup youtube.com 127.0.0.1 >/dev/null 2>&1")[0] == 0
        if not ok:
            time.sleep(4)
            ok = conn.run("nslookup youtube.com 127.0.0.1 >/dev/null 2>&1")[0] == 0
        if not ok and eff != "isp":
            log("  DNS не отвечает — возвращаю DNS провайдера, чтобы не пропал интернет")
            conn.put_text("DNS_MODE=isp\n", f"{DNSCTL}/dns.conf")
            conn.run(f"sh {DNSCTL}/dnsctl.sh apply", timeout=60)
            raise StepError("Выбранный DNS не ответил (возможно, провайдер блокирует этот DoH-сервер). "
                            "Попробуйте другой сервер DNS. Сейчас включён DNS провайдера.")
        log(f"  сейчас работает: DNS {DNS_STATE_TEXT.get(eff, eff)}" + (" ✔" if ok else ""))
        if mode == "vpn" and eff != "vpn":
            log("  VPN сейчас выключен — пока работает " + DNS_STATE_TEXT.get(eff, eff)
                + "; как только VPN включится, DNS перейдёт в туннель")
    finally:
        if own:
            conn.close()


def dns_check(cfg, log):
    """Сравнивает ответы DNS провайдера и текущего DNS роутера: видно ли подмену."""
    conn = router_session(cfg, log, "Проверка подмены DNS")
    try:
        isp = conn.run("ubus call network.interface.wan status 2>/dev/null | "
                       "jsonfilter -e '@[\"dns-server\"][0]'")[1].strip()
        if not isp:
            isp = conn.run("ubus call network.interface.wan_6 status 2>/dev/null | "
                           "jsonfilter -e '@[\"dns-server\"][0]'")[1].strip()
        eff = conn.run(f"cat {DNSCTL}/state 2>/dev/null")[1].strip() or "isp"
        log(f"  DNS провайдера: {isp or 'не найден'}; на роутере сейчас: {DNS_STATE_TEXT.get(eff, eff)}")
        spoof = 0
        for dom in ("youtube.com", "instagram.com", "rutracker.org", "discord.com"):
            def ask(server):
                out = conn.run(f"nslookup {dom} {server} 2>/dev/null", timeout=20)[1]
                ips = re.findall(r"Address(?: \d+)?:\s*(\d+\.\d+\.\d+\.\d+)", out.split("Name:", 1)[-1])
                return [i for i in ips if i != server]
            a = ask(isp) if isp else []
            b = ask("127.0.0.1")
            bad = (not a and b) or any(re.match(r"(10\.|127\.|0\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", i) for i in a)
            spoof += bool(bad)
            log(f"  {dom}: провайдер → {', '.join(a[:2]) or 'нет ответа'}; роутер → {', '.join(b[:2]) or 'нет ответа'}"
                + ("   ⚠ похоже на подмену" if bad else ""))
        log("  Итог: " + ("провайдер подменяет DNS — используйте зашифрованный DNS или DNS через VPN"
                          if spoof else "явной подмены не видно (адреса могут отличаться — это нормально для CDN)"))
    finally:
        conn.close()


# ---------------------------------------------------------------- Zapret
ZAPRET_REPO = "https://github.com/remittor/zapret-openwrt"


def zapret_find_asset(arch, log):
    """Находит на GitHub zip с пакетами zapret под архитектуру роутера. Возвращает (тег, имя, url)."""
    hdr = {"User-Agent": "openwrt-installer"}
    try:
        r = urllib.request.urlopen(urllib.request.Request(ZAPRET_REPO + "/releases/latest", headers=hdr), timeout=30)
        tag = r.geturl().rstrip("/").rsplit("/", 1)[-1]
        r.close()
        page = urllib.request.urlopen(urllib.request.Request(
            f"{ZAPRET_REPO}/releases/expanded_assets/{tag}", headers=hdr), timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise StepError(f"Ноутбук не смог зайти на GitHub ({e}).\n"
                        "Нужен интернет на ноутбуке: нажмите «Вернуть DHCP», подождите минуту и повторите.")
    pat = r'href="([^"]*/releases/download/[^"]*/(zapret_[^"/]*_' + re.escape(arch) + r'\.zip))"'
    m = re.search(pat, page)
    if not m:
        # «последним» может оказаться zapret2 — ищем свежий релиз обычного zapret
        try:
            rel = urllib.request.urlopen(urllib.request.Request(ZAPRET_REPO + "/releases", headers=hdr),
                                         timeout=30).read().decode("utf-8", "replace")
            for t in list(dict.fromkeys(re.findall(r'/releases/tag/([^"?#]+)"', rel)))[:8]:
                pg = urllib.request.urlopen(urllib.request.Request(
                    f"{ZAPRET_REPO}/releases/expanded_assets/{t}", headers=hdr), timeout=30).read().decode("utf-8", "replace")
                m = re.search(pat, pg)
                if m:
                    tag = t
                    break
        except (urllib.error.URLError, OSError, ValueError):
            pass
    if not m:
        raise StepError(f"На GitHub не найдена сборка zapret для архитектуры «{arch}».")
    href = m.group(1)
    url = href if href.startswith("http") else "https://github.com" + href
    return tag, m.group(2), url


def zapret_install(cfg, log):
    router, user, pw = cfg["router"], cfg["user"], cfg["password"]
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    log(f"Zapret 1/5: подключение к {router}")
    if not wait_ping(router, True, 20):
        raise StepError(f"Роутер {router} не отвечает. Кабель в LAN, роутер загружен?")
    conn = connect_any(router, user, pw)
    tmp = tempfile.mkdtemp(prefix="zapret_")
    try:
        if conn.run("[ -x /etc/init.d/zapret ]")[0] == 0:
            log("  zapret уже установлен, просто включаю")
            conn.run("/etc/init.d/zapret enable; /etc/init.d/zapret restart", timeout=120)
            time.sleep(4)
            ok = conn.run("pidof nfqws >/dev/null || pidof tpws >/dev/null")[0] == 0
            log("  zapret запущен" if ok else "  ВНИМАНИЕ: процесс zapret не найден, смотрите: logread | grep -i zapret")
            return
        code, out = conn.run("ping -c 2 -W 3 1.1.1.1")
        if code != 0:
            raise StepError("У роутера нет интернета (нужен для зависимостей zapret). Настройте WAN.")
        pm = "apk" if conn.run("command -v apk")[0] == 0 else "opkg"
        ext = ".apk" if pm == "apk" else ".ipk"
        code, out = conn.run(". /etc/openwrt_release; echo $DISTRIB_ARCH")
        arch = out.strip().split()[-1] if out.strip() else ""
        if not arch:
            raise StepError("Не удалось определить архитектуру роутера.")
        log(f"  менеджер пакетов: {pm}, архитектура: {arch}")

        log("Zapret 2/5: поиск и скачивание пакетов с GitHub (на ноутбуке)")
        tag, name, url = zapret_find_asset(arch, log)
        log(f"  релиз {tag}: {name}")
        zpath = os.path.join(tmp, name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "openwrt-installer"})
            with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except (urllib.error.URLError, OSError) as e:
            raise StepError(f"Не удалось скачать {name}: {e}")
        pkgs = {}
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                base = os.path.basename(info.filename)
                if not base.endswith(ext) or not re.fullmatch(r"[A-Za-z0-9._+-]+", base):
                    continue
                if re.match(r"zapret[-_]", base):
                    kind = "base"
                elif re.match(r"luci-app-zapret", base):
                    kind = "luci"
                else:
                    continue
                dest = os.path.join(tmp, base)
                with z.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                pkgs[kind] = dest
        if "base" not in pkgs or "luci" not in pkgs:
            raise StepError(f"В архиве нет пакетов zapret и luci-app-zapret для {pm}. Найдено: {sorted(pkgs)}")

        log("Zapret 3/5: загрузка пакетов на роутер")
        remote = []
        for kind in ("base", "luci"):
            fn = os.path.basename(pkgs[kind])
            conn.put(pkgs[kind], "/tmp/" + fn, log)
            remote.append("/tmp/" + fn)

        log("Zapret 4/5: установка (подтягиваются зависимости, пара минут)")
        conn.run("apk update" if pm == "apk" else "opkg update", timeout=300)
        for path in remote:
            if pm == "apk":
                cmd = f"apk add --allow-untrusted '{path}'"
                code, out = conn.run(cmd, timeout=600)
                if code != 0 and "non-repository" in out:
                    code, out = conn.run(f"apk add --allow-untrusted --force-non-repository '{path}'", timeout=600)
            else:
                code, out = conn.run(f"opkg install '{path}'", timeout=600)
            log(_tail(out, 6))
            if code != 0:
                raise StepError(f"Не удалось установить {os.path.basename(path)}.\n" + _tail(out, 6))
        conn.run("rm -f " + " ".join(f"'{p}'" for p in remote))

        log("Zapret 5/5: включение")
        if conn.run("[ -x /etc/init.d/zapret ]")[0] != 0:
            raise StepError("После установки не найдена служба zapret.")
        conn.run("/etc/init.d/zapret enable; /etc/init.d/zapret restart", timeout=120)
        time.sleep(5)
        ok = conn.run("pidof nfqws >/dev/null || pidof tpws >/dev/null")[0] == 0
        log("  zapret запущен" if ok else "  ВНИМАНИЕ: процесс zapret не найден (nfqws/tpws), смотрите: logread | grep -i zapret")
        log("ГОТОВО. Стратегию можно менять в панели: Services → Zapret (после правок нажмите Restart). "
            "Если VPN перестал работать после включения zapret, нажмите «Отключить zapret».")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        conn.close()


def zapret_disable(cfg, log, quiet=False):
    router, user, pw = cfg["router"], cfg["user"], cfg["password"]
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not wait_ping(router, True, 20):
        raise StepError(f"Роутер {router} не отвечает.")
    conn = connect_any(router, user, pw)
    try:
        if conn.run("[ -x /etc/init.d/zapret ]")[0] != 0:
            if quiet:
                return
            raise StepError("Zapret на роутере не установлен.")
        conn.run("/etc/init.d/zapret stop; /etc/init.d/zapret disable", timeout=60)
        log("Zapret остановлен и убран из автозапуска (пакеты остались, включить можно снова).")
    finally:
        conn.close()


# ---------------------------------------------------------------- Восстановление из бэкапа
RESTORE_ORDER = ("ubi", "u-boot-env", "FIP")      # FIP (загрузчик) — строго последним


def load_backup(folder):
    """Читает папку backup_*: возвращает {имя_раздела: (путь, размер_raw, md5_raw)} для нужных разделов."""
    if not os.path.isdir(folder):
        raise StepError("Папка бэкапа не найдена.")
    found = {}
    for fn in os.listdir(folder):
        m = re.match(r"^mtd\d+_(.+?)\.bin(\.gz)?$", fn)
        if m:
            found[m.group(1)] = os.path.join(folder, fn)
    for need in ("ubi", "FIP"):
        if need not in found:
            raise StepError(f"В папке бэкапа нет файла для раздела «{need}». Выбрана верная папка backup_*?")
    info = {}
    for name in RESTORE_ORDER:
        path = found.get(name)
        if not path:
            continue
        h, n = hashlib.md5(), 0
        try:
            op = gzip.open if path.endswith(".gz") else open
            with op(path, "rb") as f:
                while True:
                    c = f.read(1 << 20)
                    if not c:
                        break
                    h.update(c)
                    n += len(c)
        except (OSError, EOFError):
            raise StepError(f"Файл бэкапа повреждён: {os.path.basename(path)}")
        if n == 0:
            raise StepError(f"Файл бэкапа пуст: {os.path.basename(path)}")
        info[name] = (path, n, h.hexdigest())
    return info


def stream_to_router(conn, path, cmd, log):
    """Отправляет файл (gz) на stdin команды роутера; возвращает (код, вывод)."""
    size = os.path.getsize(path)
    ch = conn.t.open_session()
    ch.settimeout(900)
    ch.set_combine_stderr(True)
    ch.exec_command(cmd)
    sent, last = 0, -1
    with open(path, "rb") as f:
        while True:
            c = f.read(65536)
            if not c:
                break
            ch.sendall(c)
            sent += len(c)
            pct = sent * 100 // max(size, 1) // 20 * 20
            if pct != last:
                last = pct
                log(f"    передано {pct}%")
    ch.shutdown_write()
    out = b""
    while True:
        d = ch.recv(4096)
        if not d:
            break
        out += d
    return ch.recv_exit_status(), out.decode(errors="replace").strip()


def restore_backup(cfg, log):
    adapter, router, user, pw = cfg["adapter"], cfg["router"], cfg["user"], cfg["password"]
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not is_admin():
        raise StepError(ADMIN_HINT)
    if not adapter:
        raise StepError("Выберите сетевой адаптер (тот, в который воткнут кабель роутера).")
    log(f"Восстановление 1/6: проверка бэкапа в {cfg['folder']}")
    info = load_backup(cfg["folder"])
    for name, (path, n, md5) in info.items():
        log(f"  {name}: {os.path.basename(path)}, {n / 1048576:.1f} МБ")
    files = find_files()
    if not files["recovery"]:
        raise StepError("Нужен файл *initramfs-recovery.itb в папке firmware (чтобы загрузить роутер из памяти).")

    log(f"Восстановление 2/6: адаптер «{adapter}» -> {PC_IP}/{PC_MASK}, запуск TFTP")
    code, out = set_static_ip(adapter)
    if code != 0:
        raise StepError(f"Не удалось задать IP {PC_IP} на «{adapter}»: {out}")
    fw_allow(FW_RULE, "UDP", 69)
    time.sleep(4)
    tftp = TftpServer(PC_IP, files["recovery"], log)
    try:
        tftp.start()
    except OSError as e:
        raise StepError(f"Не удалось запустить TFTP на {PC_IP}:69 ({e}). Закройте tftpd64/другие TFTP-серверы.")
    conn = None
    try:
        log(f"Восстановление 3/6: подключение к {router}")
        if not wait_ping(router, True, 40):
            raise StepError(f"Роутер {router} не отвечает. Проверьте кабель в LAN и адаптер.")
        conn = connect_any(router, user, pw)
        in_flash = conn.run("grep -q ubifs /proc/mounts")[0] == 0

        def check_partitions():
            code, out = conn.run("cat /proc/mtd")
            parts = {n: (d, sz) for d, sz, n in parse_proc_mtd(out)}
            for name, (path, n, md5) in info.items():
                if name not in parts:
                    raise StepError(f"На роутере нет раздела «{name}» (есть: {', '.join(sorted(parts))}).")
                if parts[name][1] != n:
                    raise StepError(f"Размер раздела «{name}» на роутере ({parts[name][1]}) не совпадает "
                                    f"с бэкапом ({n}). Бэкап от другого роутера? Остановлено, ничего не изменено.")
            return parts
        parts = check_partitions()
        log("  разделы роутера совпадают с бэкапом")

        if in_flash:
            log("Восстановление 4/6: роутер работает из флеша — стираю ubi и перезагружаю в recovery")
            code, out = conn.run("mtd erase ubi", timeout=300)
            log("  " + out.replace("\n", "\n  "))
            if code != 0:
                raise StepError(f"mtd erase ubi завершился с ошибкой (код {code}).")
            try:
                conn.run("reboot", timeout=5)
            except Exception:
                pass
            conn.close()
            conn = None
            wait_ping(router, False, 90)
            log("  жду, пока роутер скачает recovery по TFTP (до 5 мин)...")
            if not tftp.done.wait(300):
                raise StepError("Роутер не скачал recovery за 5 минут. Проверьте кабель в LAN и IP 192.168.1.254.")
            end = time.time() + 300
            while time.time() < end and not tcp_open(router, 22):
                time.sleep(3)
            time.sleep(10)
            conn = connect_retry(router, [("root", [""])], log)
            parts = check_partitions()
        else:
            log("Восстановление 4/6: роутер уже в recovery (из памяти), стирание не нужно")

        log("Восстановление 5/6: запись разделов (НЕ ВЫКЛЮЧАЙТЕ ПИТАНИЕ!)")
        for name in RESTORE_ORDER:
            if name not in info:
                continue
            path, n, md5 = info[name]
            log(f"  запись «{name}» ({n / 1048576:.1f} МБ)")
            cmd = (f"gunzip -c | mtd -e {name} write - {name}" if path.endswith(".gz")
                   else f"mtd -e {name} write - {name}")
            code, out = stream_to_router(conn, path, cmd, log)
            if out:
                log("    " + out.replace("\n", "\n    "))
            if code != 0:
                raise StepError(f"Запись «{name}» завершилась с ошибкой (код {code}). Роутер НЕ перезагружайте и "
                                "не выключайте, запустите восстановление ещё раз.")
            if name != "ubi":
                dev = parts[name][0]
                code, out = conn.run(f"cat /dev/{dev} | md5sum", timeout=120)
                if not out.split() or out.split()[0].lower() != md5:
                    raise StepError(f"«{name}»: проверка после записи не совпала. Не выключайте и не перезагружайте "
                                    "роутер, запустите восстановление ещё раз.")
                log("    проверка записи: OK")

        log("Восстановление 6/6: перезагрузка")
        try:
            conn.run("reboot", timeout=5)
        except Exception:
            pass
        log("ГОТОВО. Роутер загружается с родной прошивкой (до нескольких минут). "
            "Нажмите «Вернуть DHCP» и откройте адрес родной панели (обычно 192.168.1.1).")
    finally:
        if conn:
            conn.close()
        tftp.stop()
        fw_remove(FW_RULE, "UDP", 69)


# ---------------------------------------------------------------- Zapret: автоподбор стратегии
YT_HOSTS = "youtube.com,googlevideo.com,ytimg.com,ggpht.com,youtu.be,youtube-nocookie.com,youtubei.googleapis.com"
DC_HOSTS = "discord.com,discord.gg,discordapp.com,discordapp.net,discord.media,discordcdn.com"
# Что проверяет blockcheck. Обычная страница youtube.com у многих провайдеров открывается и без обхода
# (замедляют картинки и видео), поэтому проверяем скачивание картинки целиком (режим GET).
YT_TEST_URL = "i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
TUNE_TARGETS = (("yt", YT_TEST_URL), ("yt", "www.youtube.com"), ("dc", "discord.com"))
# Проверенные сообществом стратегии nfqws — пробуются, если blockcheck ничего не нашёл
FALLBACK_STRATEGIES = (
    "--dpi-desync=fake,multidisorder --dpi-desync-split-pos=1,midsld --dpi-desync-repeats=11 "
    "--dpi-desync-fooling=md5sig --dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com",
    "--dpi-desync=multisplit --dpi-desync-split-pos=1,sniext+1 --dpi-desync-split-seqovl=1",
    "--dpi-desync=fake --dpi-desync-fooling=badseq --dpi-desync-fake-tls-mod=rnd,dupsid,sni=www.google.com",
    "--dpi-desync=fakedsplit --dpi-desync-split-pos=1 --dpi-desync-fooling=badseq --dpi-desync-repeats=10 "
    "--dpi-desync-autottl",
    "--dpi-desync=fake,multidisorder --dpi-desync-split-pos=midsld --dpi-desync-fooling=badseq,md5sig",
    "--dpi-desync=fake,multisplit --dpi-desync-split-pos=1 --dpi-desync-fooling=md5sig --dpi-desync-repeats=6",
    "--dpi-desync=syndata,multidisorder --dpi-desync-split-pos=2",
    "--dpi-desync=multidisorder --dpi-desync-split-pos=1,midsld",
)
QUIC_OPT = "--filter-udp=443 --hostlist-domains=" + YT_HOSTS + " --dpi-desync=fake --dpi-desync-repeats=6"
CHECK_CMD = {
    "yt": f"curl -s -o /dev/null -m 12 -w '%{{http_code}} %{{size_download}}' https://{YT_TEST_URL}",
    "dc": "curl -s -o /dev/null -m 10 -w '%{http_code} %{size_download}' https://discord.com/api/v9/gateway",
}
GROUP_NAME = {"yt": "YouTube", "dc": "Discord"}


def parse_blockcheck(text):
    """Раздел SUMMARY вывода blockcheck.sh -> ({'yt': [стратегии], 'dc': [...]}, {группы, работающие без обхода})."""
    res, free = {"yt": [], "dc": []}, set()
    lines = text.splitlines()
    try:
        start = max(i for i, l in enumerate(lines) if l.strip() == "* SUMMARY")
    except ValueError:
        return res, free
    for line in lines[start + 1:]:
        line = line.strip()
        if line.startswith("* COMMON"):
            break
        m = re.match(r"^curl_test_\S+ ipv\d (\S+) : (.+)$", line)
        if not m:
            continue
        dom, val = m.group(1), m.group(2).strip()
        grp = "dc" if "discord" in dom else "yt" if re.search(r"youtube|ytimg|googlevideo|ggpht", dom) else None
        if not grp:
            continue
        if val == "working without bypass":
            free.add(grp)
            continue
        mm = re.match(r"^nfqws (.+)$", val)
        if not mm or "not working" in val or "aborted" in val:
            continue
        opts = mm.group(1).strip()
        if opts and opts not in res[grp]:
            res[grp].append(opts)
    return res, free


def build_nfqws_opt(strat, quic=True):
    """strat: {'yt': стратегия|None, 'dc': стратегия|None} -> NFQWS_OPT с профилями по доменам."""
    yt, dc = strat.get("yt"), strat.get("dc")
    parts = []
    if yt and dc and yt == dc:
        parts.append(f"--filter-tcp=443 --hostlist-domains={YT_HOSTS},{DC_HOSTS} {yt}")
    else:
        if yt:
            parts.append(f"--filter-tcp=443 --hostlist-domains={YT_HOSTS} {yt}")
        if dc:
            parts.append(f"--filter-tcp=443 --hostlist-domains={DC_HOSTS} {dc}")
    if parts and quic:
        parts.append(QUIC_OPT)
    return " --new ".join(parts)


def _check_ok(out, grp):
    p = out.strip().split()
    if len(p) < 2 or not p[0].isdigit():
        return False
    if grp == "yt":
        return p[0] == "200" and p[1].split(".")[0].isdigit() and int(p[1].split(".")[0]) >= 25000
    return p[0] == "200"


def zapret_autotune(cfg, log):
    conn = router_session(cfg, log, "Подбор стратегии 1/5")
    fk = False
    try:
        if cfg.get("only_if_new") and conn.run("[ -f /etc/zapret-autotuned ]")[0] == 0:
            log("  стратегия zapret уже подобрана раньше — пропускаю подбор "
                "(подобрать заново: «Обход блокировок» → «Zapret» → «Подобрать автоматически»)")
            return
        if conn.run("[ -x /etc/init.d/zapret ] && [ -f /opt/zapret/blockcheck.sh ]")[0] != 0:
            raise StepError("Zapret не установлен (или нет blockcheck.sh). Сначала нажмите «Установить и включить zapret».")
        if conn.run("ping -c 2 -W 3 1.1.1.1")[0] != 0:
            raise StepError("У роутера нет интернета, подбор невозможен.")
        miss = [p for p in ("/opt/zapret/nfq/nfqws", "/opt/zapret/mdig/mdig")
                if conn.run(f"[ -x {p} ]")[0] != 0]
        if miss:
            raise StepError("В пакете zapret нет нужных программ: " + ", ".join(miss)
                            + ". Переустановите zapret кнопкой «Установить и включить zapret».")
        fk = forkop_active(conn)
        if fk and conn.run(f"uci -q get forkop.{OWI}_zapret >/dev/null")[0] != 0:
            raise StepError("Работает Forkop, но в нём нет раздела zapret. Включите zapret в разделе "
                            "«Обход блокировок» и нажмите «Применить», затем повторите подбор.")
        old = _read_zapret_strategy(conn, fk)
        log("  стратегия будет записана в " + ("раздел zapret в Forkop" if fk else "настройки zapret"))
        if fk:
            conn.run("/etc/init.d/forkop stop", timeout=120)
        conn.run("/etc/init.d/zapret stop; killall nfqws 2>/dev/null; true", timeout=60)
        time.sleep(2)
        base = {g: _check_ok(conn.run(CHECK_CMD[g], timeout=30)[1], g) for g in ("yt", "dc")}
        log("  без обхода: YouTube " + ("работает" if base["yt"] else "не работает/замедлен")
            + ", Discord " + ("работает" if base["dc"] else "не работает"))

        log("Подбор стратегии 2/5: blockcheck перебирает стратегии (10–30 минут, не закрывайте программу)")
        doms = " ".join(d for _, d in TUNE_TARGETS)
        env = ("BATCH=1 SKIP_DNSCHECK=1 SKIP_IPBLOCK=1 SKIP_TPWS=1 IPVS=4 ENABLE_HTTP=0 ENABLE_HTTPS_TLS12=1 "
               "ENABLE_HTTPS_TLS13=0 ENABLE_HTTP3=0 REPEATS=1 PARALLEL=0 SCANLEVEL=quick CURL_HTTPS_GET=1 "
               f"CURL_MAX_TIME=5 DOMAINS='{doms}'")
        ch = conn.t.open_session()
        ch.settimeout(30)
        ch.set_combine_stderr(True)
        ch.exec_command(f"cd /opt/zapret && {env} sh ./blockcheck.sh 2>&1")
        buf, text, last_beat, tested = b"", [], time.time(), 0
        deadline = time.time() + 2700
        while True:
            try:
                d = ch.recv(4096)
            except socket.timeout:
                d = None
            if d == b"":
                break
            if time.time() > deadline:
                log("  blockcheck идёт слишком долго — останавливаю и беру то, что найдено")
                ch.close()
                conn.run("kill $(pgrep -f blockcheck.sh) 2>/dev/null; killall nfqws 2>/dev/null; "
                         "nft delete table inet zapret 2>/dev/null; true")
                break
            if d:
                buf += d
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode(errors="replace").rstrip()
                    text.append(line)
                    if line.startswith("- curl_test"):
                        tested += 1
                    if (line.startswith(("!!!!!", "* curl_test", "* SUMMARY")) or
                            re.search(r"not available|does not exist|exiting|please install|pls install|"
                                      r"working without bypass|aborted", line)) and len(line) < 220:
                        log("  " + line)
            if time.time() - last_beat > 60:
                last_beat = time.time()
                log(f"  ...идёт перебор (проверено стратегий: {tested})")
        try:
            with open(BLOCKCHECK_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(text))
        except OSError:
            pass
        found, free = parse_blockcheck("\n".join(text))
        if "* SUMMARY" not in text and not tested:
            tail = "\n".join("  " + l for l in text[-12:] if l.strip())
            log("  blockcheck завершился сразу. Последние строки:\n" + tail)
        log("Подбор стратегии 3/5: результат blockcheck — "
            + ", ".join(f"{GROUP_NAME[g]}: " + ("работает без обхода" if g in free else
                                                f"{len(found[g])} шт." if found[g] else "не найдено")
                        for g in ("yt", "dc"))
            + f" (полный журнал: {os.path.basename(BLOCKCHECK_LOG)})")

        def apply(value):
            zapret_apply_opt(conn, value, fk)

        def works(strat, grp):
            apply(strat[grp] if fk else build_nfqws_opt(strat, quic=False))
            if conn.run("pidof nfqws >/dev/null")[0] != 0:
                return False
            return _check_ok(conn.run(CHECK_CMD[grp], timeout=30)[1], grp)

        log("Подбор стратегии 4/5: проверка стратегий на вашем интернете")
        chosen = {}
        for g in ("yt", "dc"):
            if base[g]:
                log(f"  {GROUP_NAME[g]}: работает без обхода — стратегия не нужна")
                continue
            cands = found[g] + [s for s in FALLBACK_STRATEGIES if s not in found[g]]
            for n, s in enumerate(cands, 1):
                src = "blockcheck" if s in found[g] else "запасная"
                if works(dict(chosen, **{g: s}), g):
                    chosen[g] = s
                    log(f"  {GROUP_NAME[g]}: подошла стратегия ({src}): {s}")
                    break
                if n == len(found[g]) and found[g]:
                    log(f"  {GROUP_NAME[g]}: стратегии blockcheck не прошли проверку, пробую запасные")
            else:
                log(f"  {GROUP_NAME[g]}: ни одна стратегия не помогла")

        if not chosen:
            apply(old.strip() or ("" if fk else "--filter-tcp=443 --dpi-desync=fake"))
            if all(base.values()):
                raise StepError("YouTube и Discord у вас работают и без zapret — обход не нужен.")
            raise StepError("Не удалось подобрать стратегию: ни одна не помогла с вашим провайдером.\n"
                            "Используйте режим 2 (VPN) — либо пришлите файл blockcheck_log.txt "
                            "из папки программы, разберёмся.")
        if fk:                      # в Forkop раздел уже ограничен списками YouTube/Discord — нужна сама стратегия
            y, d = chosen.get("yt"), chosen.get("dc")
            if y and d and y != d:   # разные стратегии — Discord в отдельный раздел Forkop
                conn.run(f"uci -q del_list forkop.{OWI}_zapret.community_lists=discord; "
                         f"uci set forkop.{OWI}_zapret_dc=section; "
                         f"uci set forkop.{OWI}_zapret_dc.label={shq('Discord через zapret')}; "
                         f"uci set forkop.{OWI}_zapret_dc.enabled=1; uci set forkop.{OWI}_zapret_dc.action=zapret; "
                         f"uci add_list forkop.{OWI}_zapret_dc.community_lists=discord; "
                         f"uci set forkop.{OWI}_zapret_dc.nfqws_opt={shq(d)}; uci commit forkop")
                log("  Discord вынесен в отдельный раздел Forkop со своей стратегией")
            apply(y or d)
            conn.run("date > /etc/zapret-autotuned")
        else:
            opt = build_nfqws_opt(chosen, quic=True)
            apply(opt)
            if conn.run("pidof nfqws >/dev/null")[0] != 0:
                log("  nfqws не запустился с профилем QUIC — применяю без него")
                opt = build_nfqws_opt(chosen, quic=False)
                apply(opt)
            conn.run("/etc/init.d/zapret enable; date > /etc/zapret-autotuned")
        log("Подбор стратегии 5/5: итоговая проверка")
        for g in ("yt", "dc"):
            ok = _check_ok(conn.run(CHECK_CMD[g], timeout=30)[1], g)
            log(f"  {GROUP_NAME[g]}: {'работает ✔' if ok else 'НЕ работает'}")
        log("ГОТОВО: стратегия сохранена на роутере и переживёт перезагрузку.")
    finally:
        if fk:
            conn.run("pidof sing-box >/dev/null || /etc/init.d/forkop start", timeout=120)
        conn.close()


# ---------------------------------------------------------------- Forkop (готовое решение на sing-box)
# https://github.com/ushan0v/forkop — форк Podkop: правила по сайтам, подписки, URLTest, zapret/ByeDPI, панель LuCI.
FORKOP_INSTALL_URLS = ("https://raw.githubusercontent.com/ushan0v/forkop/main/install.sh",
                       "https://forkop.sourceforge.io/install.sh")
FORKOP_LUCI_PATH = "/cgi-bin/luci/admin/services/forkop"
OWI = "owi"                 # префикс наших разделов в /etc/config/forkop — чужие разделы не трогаем
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_stream(conn, cmd, log, timeout=900, prefix="  "):
    """Выполняет долгую команду на роутере, показывая вывод построчно. -> (код, весь вывод)."""
    ch = conn.t.open_session()
    ch.settimeout(30)
    ch.set_combine_stderr(True)
    ch.exec_command(cmd)
    buf, lines, deadline = b"", [], time.time() + timeout
    while True:
        try:
            d = ch.recv(4096)
        except socket.timeout:
            d = None
        if d == b"":
            break
        if time.time() > deadline:
            ch.close()
            raise StepError("Команда на роутере выполняется слишком долго — прервано.")
        if d:
            buf += d
            while b"\n" in buf or b"\r" in buf:
                raw, buf = re.split(rb"\r?\n|\r", buf, maxsplit=1)
                line = ANSI_RE.sub("", raw.decode(errors="replace")).rstrip()
                if line.strip():
                    lines.append(line)
                    log(prefix + line[:220])
    if buf.strip():
        line = ANSI_RE.sub("", buf.decode(errors="replace")).rstrip()
        lines.append(line)
        log(prefix + line[:220])
    return ch.recv_exit_status(), "\n".join(lines)


def builtin_vpn_cleanup(conn, log, purge=False):
    """Выключает встроенный VPN (xray, сторож, DNS-модуль, отдельный zapret), чтобы не мешал Forkop.
    purge=True — ещё и удаляет xray и geo-базы, освобождая флеш."""
    conn.run("[ -x /etc/init.d/xray-vpn ] && { /etc/init.d/xray-vpn stop; /etc/init.d/xray-vpn disable; }; "
             "nft delete table inet xray 2>/dev/null; "
             f"[ -f /etc/crontabs/root ] && sed -i '\\#{XD}/#d' /etc/crontabs/root && /etc/init.d/cron restart; true",
             timeout=60)
    conn.run(f"if [ -f {DNSCTL}/dnsctl.sh ]; then echo DNS_MODE=isp > {DNSCTL}/dns.conf; "
             f"sh {DNSCTL}/dnsctl.sh apply; /etc/init.d/dnsctl disable; fi; nft delete table inet dnsctl 2>/dev/null; true",
             timeout=60)
    conn.run("[ -x /etc/init.d/zapret ] && { /etc/init.d/zapret stop; /etc/init.d/zapret disable; }; true", timeout=60)
    log("  встроенный VPN, сторож и наш DNS-модуль выключены (Forkop делает всё это сам)")
    if purge:
        rm = "apk del" if conn.run("command -v apk")[0] == 0 else "opkg remove"
        conn.run(f"rm -rf {XD} /etc/init.d/xray-vpn; "
                 f"{rm} xray-core >/dev/null 2>&1; {rm} v2ray-geoip >/dev/null 2>&1; "
                 f"{rm} v2ray-geosite >/dev/null 2>&1; true", timeout=300)
        free = conn.run("df -k /overlay | awk 'NR==2{print int($4/1024)}'")[1].strip()
        log(f"  xray и geo-базы удалены, свободно на флеше: {free or '?'} МБ")


def forkop_installed(conn):
    return conn.run("[ -x /usr/bin/forkop ] && [ -x /etc/init.d/forkop ]")[0] == 0


def _needs_extended(cfg):
    links = [cfg.get("vless", "")] + list(cfg.get("servers") or [])
    return any(re.search(r"[?&]type=(xhttp|splithttp)\b", l or "") for l in links)


def forkop_configure(conn, cfg, log):
    """Записывает наши разделы (owi_*) в /etc/config/forkop по режиму и ключу, перезапускает Forkop."""
    mode = cfg.get("mode") or "vpn"
    link = (cfg.get("vless") or "").strip()
    sub = (cfg.get("sub_url") or cfg.get("sub") or "").strip()
    if not is_sub_url(sub):
        sub = ""
    if link:
        build_xray_config(link)            # проверка формата до записи на роутер
    q = shq
    cmds = [f"uci -q delete forkop.{OWI}_zapret", f"uci -q delete forkop.{OWI}_zapret_dc",
            f"uci -q delete forkop.{OWI}_vpn",
            f"for s in $(uci -q -X show forkop | sed -n \"s/^forkop\\.\\([^.]*\\)\\.section='{OWI}_vpn'$/\\1/p\"); "
            "do uci -q delete forkop.$s; done"]
    if mode in ("zapret", "both"):
        if conn.run("[ -x /etc/init.d/zapret ] || [ -d /opt/zapret ]")[0] != 0:
            log("  установка zapret для Forkop")
            run_stream(conn, "forkop component_action zapret install 2>&1", log, timeout=900)
        cmds += [f"uci set forkop.{OWI}_zapret=section",
                 f"uci set forkop.{OWI}_zapret.label={q('YouTube и Discord через zapret')}",
                 f"uci set forkop.{OWI}_zapret.enabled=1",
                 f"uci set forkop.{OWI}_zapret.action=zapret",
                 f"uci add_list forkop.{OWI}_zapret.community_lists=youtube",
                 f"uci add_list forkop.{OWI}_zapret.community_lists=discord"]
    has_key = bool(link or sub)
    if mode in ("vpn", "both") and has_key:
        lists = ["russia_inside", "geoblock", "google_ai"] + (["youtube", "discord"] if mode == "vpn" else [])
        cmds += [f"uci set forkop.{OWI}_vpn=section",
                 f"uci set forkop.{OWI}_vpn.label={q('VPN: заблокированное в РФ')}",
                 f"uci set forkop.{OWI}_vpn.enabled=1",
                 f"uci set forkop.{OWI}_vpn.action=connection"]
        cmds += [f"uci add_list forkop.{OWI}_vpn.community_lists={x}" for x in lists]
        if sub:
            cmds += ["S=$(uci add forkop subscription_url)",
                     f"uci set forkop.$S.section={OWI}_vpn",
                     f"uci set forkop.$S.url={q(sub)}",
                     "uci set forkop.$S.subscription_update_enabled=1",
                     "uci set forkop.$S.subscription_update_interval=6h",
                     "uci set forkop.$S.include_urltest_groups=1"]
        else:
            cmds.append(f"uci add_list forkop.{OWI}_vpn.selector_proxy_links={q(link)}")
    elif mode in ("vpn", "both"):
        log("  ВНИМАНИЕ: ключа VPN нет — раздел VPN в Forkop не создан (вставьте ключ или подписку в разделе «Обход блокировок»)")
    cmds.append("uci commit forkop")
    code, out = conn.run("; ".join(cmds), timeout=60)
    if code != 0:
        raise StepError("Не удалось записать настройки Forkop:\n" + _tail(out, 4))
    log("  разделы записаны: " + ", ".join(
        (["zapret: YouTube, Discord"] if mode in ("zapret", "both") else []) +
        (["VPN: заблокированное в РФ, гео-блок, нейросети Google" + (", YouTube, Discord" if mode == "vpn" else "")]
         if mode in ("vpn", "both") and has_key else [])) )
    conn.run("/etc/init.d/forkop enable; /etc/init.d/forkop restart", timeout=180)
    time.sleep(5)
    if sub:
        log("  загрузка подписки в Forkop")
        run_stream(conn, "forkop subscription_update 2>&1 | tail -n 5", log, timeout=180)
    running = conn.run("pidof sing-box >/dev/null")[0] == 0
    log("  Forkop " + ("работает ✔" if running else "НЕ запустился — откройте панель Forkop, там будет причина"))
    if has_key and running:
        code, out = conn.run("forkop check_proxy 2>&1 | tail -n 3", timeout=60)
        if out.strip():
            log(_tail(out, 3))


def forkop_install(cfg, log):
    """Ставит Forkop официальным установщиком (без вопросов), выключает встроенный VPN и настраивает разделы."""
    conn = router_session(cfg, log, "Forkop 1/4")
    try:
        rel = conn.run(". /etc/openwrt_release; echo $DISTRIB_RELEASE")[1].strip()
        m = re.match(r"(\d+)", rel)
        if m and int(m.group(1)) < 24:
            raise StepError(f"Forkop требует OpenWrt 24.10 или новее, а на роутере {rel}.")
        if conn.run("ping -c 2 -W 3 1.1.1.1")[0] != 0:
            raise StepError("У роутера нет интернета — Forkop ставится из интернета.")
        log("Forkop 2/4: выключаю встроенный VPN")
        builtin_vpn_cleanup(conn, log, purge=bool(cfg.get("purge_xray")))
        if forkop_installed(conn) and not cfg.get("reinstall"):
            log("Forkop 3/4: уже установлен — обновление можно сделать в его панели")
        else:
            log("Forkop 3/4: установка официальным установщиком (несколько минут)")
            dl = " || ".join(f"wget -q -O /tmp/forkop-install.sh {shq(u)} || "
                             f"uclient-fetch -q -O /tmp/forkop-install.sh {shq(u)}" for u in FORKOP_INSTALL_URLS)
            code, out = conn.run(f"rm -f /tmp/forkop-install.sh; {dl}; [ -s /tmp/forkop-install.sh ]", timeout=120)
            if code != 0:
                raise StepError("Не удалось скачать установщик Forkop (GitHub недоступен с роутера?).")
            # stdin не терминал -> установщик сам выбирает ответы по умолчанию (русский язык, sing-box stable)
            code, out = run_stream(conn, "sh /tmp/forkop-install.sh </dev/null 2>&1", log, timeout=1200)
            if not forkop_installed(conn):
                raise StepError("Forkop не установился. Последние строки:\n" + _tail(out, 6))
        if cfg.get("singbox_extended") or _needs_extended(cfg):
            ver = conn.run("sing-box version 2>/dev/null | head -n 1")[1]
            if "extended" not in ver:
                log("  ставлю sing-box extended (нужен для ключей с XHTTP)")
                run_stream(conn, "forkop component_action sing_box install_extended 2>&1", log, timeout=900)
        if cfg.get("configure", True):
            log("Forkop 4/4: настройка")
            forkop_configure(conn, cfg, log)
        else:
            log("Forkop 4/4: установлен; разделы настроятся на шаге «Режим» (или кнопкой «Применить» в разделе «Обход блокировок»)")
        log(f"ГОТОВО. Тонкие правила (какой сайт через какой сервер) — в панели: http://{cfg['router']}{FORKOP_LUCI_PATH}")
    finally:
        conn.close()


def forkop_apply(cfg, log):
    """Режим и ключ для уже установленного Forkop (без переустановки)."""
    conn = router_session(cfg, log, "Forkop")
    try:
        if not forkop_installed(conn):
            conn.close()
            conn = None
            return forkop_install(cfg, log)
        builtin_vpn_cleanup(conn, log)
        forkop_configure(conn, cfg, log)
    finally:
        if conn:
            conn.close()


def forkop_disable(cfg, log, conn=None):
    own = conn is None
    if own:
        conn = router_session(cfg, log, "Forkop: выключение")
    try:
        if forkop_installed(conn):
            conn.run("/etc/init.d/forkop stop; /etc/init.d/forkop disable", timeout=120)
            log("  Forkop остановлен и убран из автозапуска (настройки и пакеты остались)")
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- Компоненты: что стоит на роутере, установка и удаление
COMPONENTS = (
    ("xray", "Встроенный VPN (xray + geo-базы)"),
    ("forkop", "Forkop (sing-box, правила по сайтам)"),
    ("zapret", "Zapret (обход DPI без VPN)"),
    ("doh", "Зашифрованный DNS (https-dns-proxy)"),
    ("luci_ru", "Русский язык веб-панели"),
)


def forkop_active(conn):
    return forkop_installed(conn) and conn.run("[ -e /etc/rc.d/S99forkop ] && pidof sing-box >/dev/null")[0] == 0


def _pm_del(conn):
    return "apk del" if conn.run("command -v apk")[0] == 0 else "opkg remove"


def components_status(cfg, log):
    """-> {'items': {ключ: (установлен, работает, пояснение)}, 'free': текст}"""
    conn = router_session(cfg, log, "Проверка компонентов")
    try:
        def ok(cmd):
            return conn.run(cmd)[0] == 0
        items = {}
        x_inst = ok("[ -x /usr/bin/xray ]")
        x_run = ok("[ -e /etc/rc.d/S99xray-vpn ] && pidof xray >/dev/null")
        items["xray"] = (x_inst, x_run, "")
        f_inst = forkop_installed(conn)
        f_run = f_inst and ok("[ -e /etc/rc.d/S99forkop ] && pidof sing-box >/dev/null")
        ver = conn.run("forkop show_version 2>/dev/null | head -n 1")[1].strip() if f_inst else ""
        items["forkop"] = (f_inst, f_run, ver)
        z_inst = ok("[ -x /etc/init.d/zapret ] || [ -d /opt/zapret ]")
        z_run = ok("pidof nfqws >/dev/null")
        items["zapret"] = (z_inst, z_run, "через Forkop" if (z_run and f_run) else "")
        d_inst = ok("[ -x /etc/init.d/https-dns-proxy ]")
        items["doh"] = (d_inst, d_inst and ok("pidof https-dns-proxy >/dev/null"), "")
        items["luci_ru"] = (ok("ls /usr/lib/lua/luci/i18n/base.ru.lmo /usr/share/luci/i18n/base.ru.* >/dev/null 2>&1 "
                               "|| [ \"$(uci -q get luci.main.lang)\" = ru ]"), True, "")
        fl = conn.run("df -k /overlay | awk 'NR==2{print int($4/1024)}'")[1].strip()
        mem = conn.run("awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo")[1].strip()
        rel = conn.run(". /etc/openwrt_release; echo $DISTRIB_RELEASE")[1].strip()
        for key, title in COMPONENTS:
            inst, run, extra = items[key]
            log(f"  {title}: " + ("установлен" + (", работает" if run else ", выключен") if inst else "не установлен")
                + (f" ({extra})" if extra else ""))
        free = f"OpenWrt {rel or '?'} · свободно: флеш {fl or '?'} МБ, память {mem or '?'} МБ"
        log("  " + free)
        return {"items": items, "free": free, "release": rel}
    finally:
        conn.close()


def xray_remove(conn, log):
    conn.run("[ -x /etc/init.d/xray-vpn ] && { /etc/init.d/xray-vpn stop; /etc/init.d/xray-vpn disable; }; "
             "nft delete table inet xray 2>/dev/null; "
             f"[ -f /etc/crontabs/root ] && sed -i '\\#{XD}/#d' /etc/crontabs/root && /etc/init.d/cron restart; "
             f"[ -f {DNSCTL}/dnsctl.sh ] && sh {DNSCTL}/dnsctl.sh apply vpn-off; true", timeout=90)
    rm = _pm_del(conn)
    conn.run(f"rm -rf {XD} /etc/init.d/xray-vpn; {rm} xray-core >/dev/null 2>&1; "
             f"{rm} v2ray-geoip >/dev/null 2>&1; {rm} v2ray-geosite >/dev/null 2>&1; true", timeout=300)
    log("  встроенный VPN удалён (xray, geo-базы, сторож, ключи на роутере)")


def forkop_remove(conn, log):
    rm = _pm_del(conn)
    conn.run("[ -x /etc/init.d/forkop ] && /etc/init.d/forkop stop; "
             "[ -x /usr/bin/forkop ] && { /usr/bin/forkop dnsmasq_restore; /usr/bin/forkop uninstall; }; true",
             timeout=180)
    conn.run(f"{rm} luci-i18n-forkop-ru >/dev/null 2>&1; {rm} luci-app-forkop >/dev/null 2>&1; "
             f"{rm} forkop >/dev/null 2>&1; {rm} sing-box-extended >/dev/null 2>&1; {rm} sing-box >/dev/null 2>&1; "
             f"{rm} sing-box-tiny >/dev/null 2>&1; rm -f /etc/config/forkop; "
             "rm -rf /tmp/luci-indexcache* /tmp/luci-modulecache; /etc/init.d/dnsmasq restart; true", timeout=300)
    log("  Forkop и sing-box удалены, настройки DNS восстановлены")


def zapret_remove(conn, log):
    rm = _pm_del(conn)
    if forkop_active(conn) and conn.run(f"uci -q get forkop.{OWI}_zapret >/dev/null")[0] == 0:
        conn.run(f"uci -q delete forkop.{OWI}_zapret; uci commit forkop; /etc/init.d/forkop restart", timeout=180)
        log("  раздел zapret в Forkop удалён")
    conn.run("[ -x /etc/init.d/zapret ] && { /etc/init.d/zapret stop; /etc/init.d/zapret disable; }; "
             f"{rm} luci-app-zapret >/dev/null 2>&1; {rm} zapret >/dev/null 2>&1; "
             "rm -f /etc/zapret-autotuned; true", timeout=300)
    log("  zapret удалён")


def doh_remove(conn, log):
    conn.run(f"{_pm_del(conn)} https-dns-proxy >/dev/null 2>&1; "
             f"[ -f {DNSCTL}/dnsctl.sh ] && sh {DNSCTL}/dnsctl.sh apply; /etc/init.d/dnsmasq restart; true", timeout=180)
    log("  зашифрованный DNS удалён (DNS идёт через VPN, если он включён, иначе через провайдера)")


def component_action(cfg, log):
    """cfg['comp'] — ключ из COMPONENTS, cfg['action'] — install / remove."""
    comp, action = cfg["comp"], cfg["action"]
    title = dict(COMPONENTS)[comp]
    if comp == "forkop" and action == "install":
        return forkop_install(cfg, log)
    if comp == "zapret" and action == "install":
        conn = router_session(cfg, log, f"{title}: установка")
        try:
            fk = forkop_active(conn)
            if fk:
                log("  Forkop работает — ставлю zapret как его компонент")
                run_stream(conn, "forkop component_action zapret install 2>&1", log, timeout=900)
                return
        finally:
            conn.close()
        return zapret_install(cfg, log)
    if comp == "luci_ru" and action == "install":
        return luci_russian(cfg, log)
    conn = router_session(cfg, log, f"{title}: " + ("установка" if action == "install" else "удаление"))
    try:
        if action == "install":
            if comp == "xray":
                d = _xray_ready(conn)
                if d:
                    log("  xray и geo-базы уже установлены")
                else:
                    _xray_setup_packages(conn, log)
                log("  установлено. Включится кнопкой «Применить» в разделе «Обход блокировок» "
                    "(VPN: встроенный)")
            elif comp == "doh":
                _doh_setup(conn, cfg.get("doh") or "Cloudflare", log)
                ensure_dnsctl(conn)
                conn.run(f"sh {DNSCTL}/dnsctl.sh apply", timeout=60)
                log("  готово. Режим DNS выбирается в разделе «Интернет, Wi-Fi, DNS»")
        else:
            {"xray": xray_remove, "forkop": forkop_remove, "zapret": zapret_remove,
             "doh": doh_remove}.get(comp, lambda c, l: l("  удаление этого компонента не предусмотрено"))(conn, log)
    finally:
        conn.close()


def install_components(cfg, log):
    """Мастер: ставит выбранные компоненты, которых ещё нет. cfg['want'] = {'engine': xray|forkop|none,
    'zapret': bool, 'doh': bool}; cfg['have'] — результат components_status()['items']."""
    want, have = cfg["want"], cfg.get("have") or {}
    def installed(k):
        return bool(have.get(k, (False,))[0])
    if want.get("engine") == "xray" and not installed("xray"):
        component_action(dict(cfg, comp="xray", action="install"), log)
    if want.get("engine") == "forkop" and not installed("forkop"):
        forkop_install(dict(cfg, configure=False), log)
    if want.get("zapret") and not installed("zapret"):
        component_action(dict(cfg, comp="zapret", action="install"), log)
    if want.get("doh") and not installed("doh"):
        component_action(dict(cfg, comp="doh", action="install"), log)
    log("Компоненты готовы.")


# ---------------------------------------------------------------- Zapret: своя стратегия
def _read_zapret_strategy(conn, fk):
    if fk:
        return conn.run(f"uci -q get forkop.{OWI}_zapret.nfqws_opt")[1].strip()
    sec = conn.run("uci show zapret 2>/dev/null | sed -n 's/^\\(zapret\\.[^.=]*\\)\\.NFQWS_OPT=.*/\\1/p' | head -n 1")[1].strip()
    if sec:
        return conn.run(f"uci -q get {sec}.NFQWS_OPT")[1].strip()
    out = conn.run("sed -n '/^NFQWS_OPT=\"/,/\"$/p' /opt/zapret/config 2>/dev/null")[1]
    return out.replace("NFQWS_OPT=", "").strip().strip('"')


def zapret_apply_opt(conn, value, fk):
    """Записывает стратегию: в раздел Forkop или в настройки отдельного zapret. Пустая строка (Forkop) — по умолчанию."""
    if fk:
        set_cmd = (f"uci set forkop.{OWI}_zapret.nfqws_opt={shq(value)}" if value
                   else f"uci -q delete forkop.{OWI}_zapret.nfqws_opt")
        conn.run(f"{set_cmd}; uci commit forkop; /etc/init.d/forkop restart", timeout=180)
        time.sleep(6)
        return
    sec = conn.run("uci show zapret 2>/dev/null | sed -n 's/^\\(zapret\\.[^.=]*\\)\\.NFQWS_OPT=.*/\\1/p' | head -n 1")[1].strip()
    if sec:
        conn.run(f"uci set {sec}.NFQWS_ENABLE=1; uci set {sec}.NFQWS_OPT={shq(value)}; uci commit zapret")
    conn.run("/etc/init.d/zapret restart", timeout=120)
    time.sleep(4)
    code, cur = conn.run("grep -c -F -- " + shq(value[:40]) + " /opt/zapret/config")
    if sec and cur.strip() not in ("", "0"):
        return
    conn.put_text(value, "/tmp/nfqws_opt.txt")
    conn.run("awk -v f=/tmp/nfqws_opt.txt 'BEGIN{getline v < f} "
             "/^NFQWS_OPT=\"/ {print \"NFQWS_OPT=\\\"\" v \"\\\"\"; skip=1; "
             "if ($0 ~ /\"$/ && length($0) > 11) skip=0; next} "
             "skip { if ($0 ~ /\"$/) skip=0; next } {print}' /opt/zapret/config > /tmp/zapret.new "
             "&& cp /tmp/zapret.new /opt/zapret/config; /etc/init.d/zapret restart", timeout=120)
    time.sleep(4)


def normalize_strategy(text):
    s = " ".join((text or "").replace("\\\n", " ").split())
    s = re.sub(r"^(nfqws2?|/\S*nfqws2?)\s+", "", s)
    s = s.strip('"').strip("'")
    if not s.startswith("--"):
        raise StepError("Стратегия — это параметры nfqws, они начинаются с «--», например:\n"
                        "--dpi-desync=fake,multidisorder --dpi-desync-split-pos=1,midsld --dpi-desync-fooling=md5sig")
    return s


def zapret_get_strategy(cfg, log):
    conn = router_session(cfg, log, "Стратегия zapret")
    try:
        fk = forkop_active(conn)
        cur = _read_zapret_strategy(conn, fk)
        log("  сейчас (" + ("Forkop" if fk else "отдельный zapret") + "): " + (cur or "по умолчанию"))
        return cur
    finally:
        conn.close()


def zapret_set_strategy(cfg, log):
    s = normalize_strategy(cfg.get("strategy"))
    conn = router_session(cfg, log, "Своя стратегия zapret")
    try:
        fk = forkop_active(conn)
        if fk:
            if conn.run(f"uci -q get forkop.{OWI}_zapret >/dev/null")[0] != 0:
                raise StepError("В Forkop нет раздела zapret. Включите zapret в разделе «Обход блокировок» "
                                "и нажмите «Применить».")
            value = s
        else:
            if conn.run("[ -x /etc/init.d/zapret ]")[0] != 0:
                raise StepError("Zapret не установлен — поставьте его в разделе «Роутер и компоненты».")
            full = re.search(r"--filter-|--hostlist|--ipset|--new\b", s)
            value = s if (full or not cfg.get("wrap", True)) else build_nfqws_opt({"yt": s, "dc": s})
            if not full and cfg.get("wrap", True):
                log("  стратегия применится только к YouTube и Discord (добавлен фильтр доменов)")
        old = _read_zapret_strategy(conn, fk)
        log("  применяю: " + value[:200] + ("…" if len(value) > 200 else ""))
        zapret_apply_opt(conn, value, fk)
        if conn.run("pidof nfqws >/dev/null")[0] != 0:
            log("  nfqws не запустился с этой стратегией — возвращаю прежнюю")
            zapret_apply_opt(conn, old, fk)
            raise StepError("Стратегия не принята (nfqws не запустился — опечатка или параметр, которого нет в "
                            "этой версии zapret). Прежняя стратегия возвращена.")
        if not fk:
            conn.run("/etc/init.d/zapret enable; date > /etc/zapret-autotuned")
        for g in ("yt", "dc"):
            good = _check_ok(conn.run(CHECK_CMD[g], timeout=30)[1], g)
            log(f"  {GROUP_NAME[g]}: {'работает ✔' if good else 'не работает'} (проверка с роутера)")
    finally:
        conn.close()


# ---------------------------------------------------------------- Режимы работы
MODE_TEXT = {
    "off": ("Обход выключен",
            "VPN и zapret выключены — весь интернет идёт напрямую через провайдера. Пакеты остаются на роутере, "
            "включить можно в любой момент."),
    "zapret": ("1. Только zapret (обход DPI, без VPN-ключа)",
               "YouTube и Discord работают через обход блокировок (zapret). Ключ VPN не нужен, скорость "
               "не падает. Не заработают сервисы, закрытые по IP-адресу или региону: большинство "
               "нейросетей, Instagram, Facebook и т. п. VPN (если был) выключается."),
    "vpn": ("2. Только VPN (нужен ключ vless)",
            "Через VPN идёт всё, кроме российских сайтов и IP (geosite:category-ru, geoip:ru), "
            "а также Google (кроме YouTube и Gemini), Apple, Microsoft и торрентов — они идут напрямую, "
            "чтобы не грузить сервер. YouTube и Discord — через VPN. Zapret выключается."),
    "both": ("3. Совместный (zapret + VPN)",
             "YouTube и Discord сначала идут через zapret — без нагрузки на VPN-сервер. Если zapret перестаёт "
             "справляться, роутер сам временно пускает их через VPN, а потом возвращает на zapret. Российские "
             "сайты, Google, Apple, Microsoft и торренты — напрямую. Остальное (Telegram, WhatsApp, Instagram, "
             "нейросети и т. д.) — через VPN. Если ключа нет, работает только zapret."),
}


def apply_mode(cfg, log):
    """Переключает режим. Уже установленное не переустанавливается — меняется только то,
    как идёт трафик (и включается/выключается нужная служба)."""
    mode = cfg["mode"]
    if cfg["vless"].strip():
        build_xray_config(cfg["vless"])          # проверить ссылку до изменений на роутере
    if mode == "off":
        log("Обход блокировок выключается")
        vpn_rollback(cfg, log)
        forkop_disable(cfg, log)
        zapret_disable(cfg, log, quiet=True)
        log("Режим применён: всё напрямую.")
        return
    if cfg.get("engine") == "forkop":
        log(f"Режим {MODE_TEXT[mode][0]} — через Forkop")
        forkop_apply(cfg, log)
        log("Режим применён.")
        return
    tune = dict(cfg, only_if_new=True)
    if mode == "zapret":
        log("Режим 1: только zapret")
        vpn_rollback(cfg, log)
        zapret_install(cfg, log)
        if cfg.get("autotune"):
            zapret_autotune(tune, log)
    elif mode == "vpn":
        log("Режим 2: только VPN")
        zapret_disable(cfg, log, quiet=True)
        vpn_install(dict(cfg, zapret_mode=False), log)
    else:
        log("Режим 3: zapret + VPN")
        zapret_install(cfg, log)
        if cfg.get("autotune"):
            try:
                zapret_autotune(tune, log)
            except StepError as e:
                log(f"  автоподбор zapret не удался: {e}")
        try:
            vpn_install(dict(cfg, zapret_mode=True), log)
        except StepError as e:
            if not str(e).startswith("Нет vless-ссылки"):
                raise
            log("Ключа VPN нет, работает только zapret. Вставьте vless-ссылку и примените режим снова.")
    if cfg.get("dns_mode"):
        try:
            dns_apply(cfg, log)
        except StepError as e:
            log(f"  ВНИМАНИЕ: DNS не настроен: {e}")
    log("Режим применён.")


# ---------------------------------------------------------------- Светодиоды
LED_SAVE = r"""[ -f /etc/leds-saved.txt ] || for l in /sys/class/leds/*; do [ -d "$l" ] || continue; n=${l##*/}; t=$(sed -n 's/.*\[\(.*\)\].*/\1/p' "$l/trigger" 2>/dev/null); b=$(cat "$l/brightness" 2>/dev/null); echo "$n|${t:-none}|${b:-0}"; done > /etc/leds-saved.txt"""

LED_OFF_SH = """#!/bin/sh
for l in /sys/class/leds/*; do
    [ -d "$l" ] || continue
    echo none > "$l/trigger" 2>/dev/null
    echo 0 > "$l/brightness" 2>/dev/null
done
"""

LED_RESTORE_SH = """#!/bin/sh
[ -f /etc/leds-saved.txt ] || exit 0
while IFS='|' read -r n t b; do
    l=/sys/class/leds/$n
    [ -d "$l" ] || continue
    echo "$t" > "$l/trigger" 2>/dev/null
    echo "$b" > "$l/brightness" 2>/dev/null
done < /etc/leds-saved.txt
"""

LED_INIT = """#!/bin/sh /etc/rc.common
START=99
start() {
    sh /etc/leds-off.sh
    ( sleep 25; sh /etc/leds-off.sh ) >/dev/null 2>&1 &
}
"""


def leds_set(cfg, log):
    router, user, pw = cfg["router"], cfg["user"], cfg["password"]
    off = cfg["leds"] == "off"
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if not wait_ping(router, True, 20):
        raise StepError(f"Роутер {router} не отвечает.")
    conn = connect_any(router, user, pw)
    try:
        code, out = conn.run("ls /sys/class/leds | wc -l")
        n = int(out.strip()) if out.strip().isdigit() else 0
        if n == 0:
            raise StepError("На роутере не найдены управляемые светодиоды (/sys/class/leds пуст).")
        if off:
            conn.run(LED_SAVE)
            conn.put_text(LED_OFF_SH, "/etc/leds-off.sh", "755")
            conn.put_text(LED_INIT, "/etc/init.d/leds-off", "755")
            conn.run("/etc/init.d/leds-off enable; sh /etc/leds-off.sh")
            log(f"Светодиоды выключены ({n} шт.) и останутся выключенными после перезагрузки.")
        else:
            conn.put_text(LED_RESTORE_SH, "/etc/leds-restore.sh", "755")
            conn.run("[ -x /etc/init.d/leds-off ] && /etc/init.d/leds-off disable; "
                     "sh /etc/leds-restore.sh; rm -f /etc/leds-saved.txt /etc/init.d/leds-off /etc/leds-off.sh "
                     "/etc/leds-restore.sh")
            log("Светодиоды включены, как было.")
    finally:
        conn.close()


# ---------------------------------------------------------------- WAN и Wi-Fi
def shq(s):
    """Безопасное экранирование строки для shell роутера."""
    return "'" + s.replace("'", "'\\''") + "'"


def build_wifi_script(ssid, key, country, split=False):
    """split=True: сети 5 ГГц получают имя с суффиксом _5G."""
    return (
        "[ -s /etc/config/wireless ] || wifi config; "
        "for r in $(uci show wireless | sed -n 's/^wireless\\.\\([^.=]*\\)=wifi-device$/\\1/p'); do "
        "uci set wireless.$r.disabled=0; uci set wireless.$r.country=" + shq(country) + "; done; "
        "for i in $(uci show wireless | sed -n 's/^wireless\\.\\([^.=]*\\)=wifi-iface$/\\1/p'); do "
        "d=$(uci -q get wireless.$i.device); b=$(uci -q get wireless.$d.band); "
        "[ -z \"$b\" ] && [ \"$(uci -q get wireless.$d.hwmode)\" = 11a ] && b=5g; "
        "s=" + shq(ssid) + "; "
        + ("[ \"$b\" = 5g ] && s=" + shq(ssid + "_5G") + "; " if split else "") +
        "uci set wireless.$i.mode=ap; uci set wireless.$i.network=lan; "
        "uci set wireless.$i.ssid=\"$s\"; uci set wireless.$i.encryption=psk2; "
        "uci set wireless.$i.key=" + shq(key) + "; uci -q delete wireless.$i.disabled; done; "
        "uci commit wireless; wifi reload"
    )


def net_setup(cfg, log):
    router, user, pw = cfg["router"], cfg["user"], cfg["password"]
    wan, ssid, wkey = cfg["wan_type"], cfg["ssid"], cfg["wifi_key"]
    country = cfg["country"].strip().upper()
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    if wan == "skip" and not ssid:
        raise StepError("Нечего применять: выберите тип WAN или введите имя Wi-Fi сети.")
    if wan == "pppoe" and not (cfg["ppp_user"] and cfg["ppp_pass"]):
        raise StepError("Введите логин и пароль PPPoE (из договора с провайдером).")
    if ssid:
        limit = 28 if cfg.get("wifi_split") else 32
        if not 1 <= len(ssid.encode("utf-8")) <= limit:
            raise StepError(f"Имя Wi-Fi сети: от 1 до {limit} байт"
                            + (" (с учётом суффикса _5G)." if cfg.get("wifi_split") else "."))
        if not 8 <= len(wkey) <= 63:
            raise StepError("Пароль Wi-Fi: от 8 до 63 символов.")
        if not re.fullmatch(r"[A-Za-z]{2}", country):
            raise StepError("Код страны Wi-Fi — две буквы, например RU.")

    log(f"Настройка: подключение к {router}")
    if not wait_ping(router, True, 20):
        raise StepError(f"Роутер {router} не отвечает. Проверьте кабель в LAN и IP ноутбука 192.168.1.254.")
    conn = connect_any(router, user, pw)
    try:
        if ssid:
            split = bool(cfg.get("wifi_split"))
            if split:
                log(f"Wi-Fi: «{ssid}» (2,4 ГГц) и «{ssid}_5G» (5 ГГц), WPA2-PSK, страна {country}")
            else:
                log(f"Wi-Fi: сеть «{ssid}» на обоих диапазонах, WPA2-PSK, страна {country}")
            conn.run(build_wifi_script(ssid, wkey, country, split), timeout=60)
            code, out = conn.run("uci show wireless | grep -c '=wifi-iface$'")
            if not out.strip().isdigit() or int(out.strip()) == 0:
                raise StepError("Wi-Fi интерфейсы на роутере не найдены (нет драйвера Wi-Fi в прошивке?).")
            time.sleep(5)
            code, out = conn.run("wifi status | grep -c '\"up\": true'")
            up = out.strip() if out.strip().isdigit() else "?"
            log(f"  Wi-Fi включён, активных радио: {up}")

        if wan != "skip":
            log("WAN: " + ("PPPoE (логин/пароль)" if wan == "pppoe" else "DHCP (автоматически)"))
            cmds = ["uci set network.wan.proto=" + shq(wan)]
            if wan == "pppoe":
                cmds += ["uci set network.wan.username=" + shq(cfg["ppp_user"]),
                         "uci set network.wan.password=" + shq(cfg["ppp_pass"])]
            else:
                cmds += ["uci -q delete network.wan.username", "uci -q delete network.wan.password"]
            cmds.append("uci commit network")
            conn.run("; ".join(cmds))
            conn.run("(/etc/init.d/network reload; sleep 2; ifup wan) >/dev/null 2>&1 &")
            log("  жду подключение к интернету (до 60 с)...")
            ok = False
            for _ in range(20):
                time.sleep(3)
                try:
                    code, out = conn.run("ping -c 1 -W 2 1.1.1.1", timeout=15)
                except Exception:
                    try:
                        conn.close()
                        conn = connect_any(router, user, pw)
                    except Exception:
                        pass
                    continue
                if code == 0:
                    ok = True
                    break
            if not ok:
                hint = ("Проверьте логин/пароль PPPoE и что кабель провайдера в порту WAN."
                        if wan == "pppoe" else
                        "Проверьте, что кабель провайдера в порту WAN (и не совпадает ли подсеть с 192.168.1.x).")
                raise StepError("Интернета на роутере пока нет. " + hint +
                                " Подробности: на роутере команда logread | tail -30")
            log("  интернет на роутере есть")
        log("ГОТОВО: настройки применены.")
    finally:
        conn.close()


# ---------------------------------------------------------------- Настройки программы (кэш полей)
SECRET_KEYS = ("password", "vless", "sub", "wifi_key", "ppp_pass", "sub_servers")


def _dpapi(data, protect):
    """Шифрование Windows (DPAPI): расшифровать может только этот пользователь на этом ПК."""
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI error")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def secret_encode(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def secret_decode(text):
    try:
        kind, data = text.split(":", 1)
        raw = base64.b64decode(data)
        if kind == "dpapi":
            raw = _dpapi(raw, False)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- GUI
C_BG, C_CARD, C_ACCENT, C_HEAD = "#eef2f7", "#ffffff", "#2563eb", "#0f172a"
C_TEXT, C_MUTED, C_OK, C_BAD, C_WARN = "#0f172a", "#475569", "#15803d", "#b91c1c", "#b45309"
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")


# Иконка программы (панк-роутер), PNG в base64 — для окна; для exe используется app.ico
ICON_64 = "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"
PANEL_ICON_64 = "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"
PANEL_ICON_32 = "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"
ICON_32 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAH2ElEQVR42qWXe4xU9RXHP+d3X/Pe3WF3WWDRhQWkAiKCEayEhoDamvhIu9RIE62JpkSDbRobrY+a1hpimlSbik1tfNMmpbXRaJtUTI3WRFQUS1ukIKCuBVlgmWV3mJl7f7/TP+6AK++UX3Jn7px75p5zvuctNI/2/d6TtcssgM596CzITwYJ0YZwJkc8BdPA1XfIe7d+dLQsGU1ozPrx3CBovydJDi7aZYdaHQ7hzOQrisHQ5ZUqgVd6PbaD94f/+OH6wzJF+/o8WbvW6nmrrsVETz8+vCF4pPImHyUVLE7TlxxlVJMmx3l2NB+Ah5GJfokVpYu4uXihw9ZvlE13PKV9fZ4oCLMfnA3+Ozfufc57YuitJOflvFA84TjWjxaq6BGETqVMQ61W7Yi9rniBt6ZzGWAvMhtvf9sIKJL5ybMj73pPDL2VdPltflYCMc1Xj74MoJp+g3Ju0IHf5FPlGP7D/xEgLwHjgrL/24Pv2t8cfEuQ8AEFjM742UTrRr7y88qbmvNyXozDpZYdY5BTKORTSQoszvTgi8GhKf24MZAqUNG6DLk6Ba/gPVRZTz0ZvkRnPNhriHJTd9vhws6kIqH44ppADmvjyL0AImAtLLmkQRAogfrMjyYQiodTZenCGM98jsRol9XV8tVM7+DscOywQ+VTO6S73UiGoHiOQV2YoEcC7vC5KT9nd1FCa1Eciip4HixdGONHliIR54edZDXED5WlCxt4fspnR0WDIBrjuCzbOzg76BpuqMWBJupAXWgQo8dLtI3xZ4UYJ1YdF0bj8ZxPPu9YcEFClLN0UKA7bKOgGYqlhPlzEnIZcE6YH0343G2otPgBdxx8efIz1fe7SibEqUtREqPmRFH7TmNXoaHWGITlhZlENqSj09LT6yi1JkyQFvCztNk8Y8cm9PRaWlosgfNZXpiJaTpCFQb2C0P7Qnz1jsmUEyqQlwCLUjIRl2UnUbBZzj4rRtpdKpAyeAGdlJjUkyDtypjOmDFaYGm2h4IJaDhHFMJ936+y6u5hSkXF2jSeTqkAQE0TJvutdIUdjKPElCkNMDC5W+n1yiDQbUpMn5qAEcaOj5ki7XSF7UwIC4wMKcuuqPOjB0eYc27CngFDEKSoHD7+iYQbhIZaZoadJCZiStRKb2+CdTBtotATtGJdTE+mSMtkxTqhuzumO+wk8SLGayu58i7uvO0QI/8VVt5bQI5jrn+yOmoMXJqZjO+yXFoez7Q54BlYMiukM9eJFwcs6mincD54Rrl4hqGQnYTvZZl2qJvytzYwbrbl7tvybNnq09HuSOzpKCCQ1B25JKS/bYiXKu8wEA1S2xjS3w/VHRm2yXaCSj/Vln2YtyM2bVH6t0Vkwz28sH8D0aQK9660VHca/r4hYP68mO0fe2hyCgVEwDUgmAgTHzasPnsdVhyeZ3CVFtQDma7oVX9MO50a9FApbTrTwSx7kT174NZXa4zLQM0qr647wNOPZfjOHUVyGSVxJ0PAAzcMY74LrZeCIwQEZ5XQU5IjIEWjWpA2PyEgolAUXu/3Wfg0vHjVIP9+xWfFnUXCQE8zDQVMa1p6nVOGNzi2zlQOvAZb58Pg2pQex444VpyDAy/Atnlw4DXl4685/nOez4y5CfGgsPzJFkTB9xWrenp1AAvigYuhMBfaboYtiyA7B9r6wCpIkF5WofVKyC1MeUo3QOsY5erNNb63r0j9OkPYcPjiubwEdjQO5lTThPhQ74ehP0HrYqiuh+p7zdacAC69r74P1TegbQkMrBOmPhvzfCXi/StCyr5STRxn+y21+eGESl2tmGY1Micfp1IU7EEoXQnTXoHyLRDvSxuT54O69F4FSn0w5WUonaN88Izh3VsiyqFSr0HGeGxN9udeqe8sZ8RX13SFfzLpAtgYsl8CUdh2DbR8E8YsgU/uA2Lo/il89muo/wsmPgwWiHpAHjVkOsHBkQIkCCb17knSMGVSCVXUpjA7YOh5aLkahv8M+1dD5rwUvy2LIOwFrwTbr0n5cxdD65chHoEgAxKeGGWfY4aHhEPqpLrJp/0bAtmUPu7O1F9t18P+NdC+PKUPrIG25RACe34H0Swozkyt9PPgAY1/isrxhyx81IkCHqLDGssVwZTB26cu+PTaR56bvjOu+fmpBtVmA2lGvtcGe59If3vtMPgUaAx+BzQ2w571zVQWGN7uGHjUiZSEjPW+EF+oEx/82EPwSOdTo6JZL3A42LsKDiBpmTmdOXx0fotQ04RpfvnQmkmXf/irkQ1dz1U/aM9JoEZE/DQLYh9T29pl8iMT/GK+Gsf619r28ku7t5VD8YjazelLPLag6ohamZXtGLmkvHj3DncgWlvd3B7j6PZKMtbk65jalnQzOv8Xf3ny4JuXf3vPH5JxQdmP1aZz4JlsZE3VDaIXRRMqm+O9+WHXCAaSSrK640p/RWnh32TjysVGQbCNu28oztPrihd4u+L9ySFNVOEMV7LDBVVlXW1H6y47HAwkleTrhVlmRWkBSO0ugM9Xs1mrrseLHl99cL15tLKeT5KhdH5FlDPEIhDDeK9obirNY2VpAWKTm2XTDx7Tvj7vC8upznxgAWHbPbXkwMLddqhgVc9wNU1V90QYa4ojWb/tDZL998umu14/spwedz2f88spSK4XjSOa6fL/GW8UDxDTIE4+lI0rth4t63/0sncFibqg/AAAAABJRU5ErkJggg=="


def setup_style(root):
    root.configure(bg=C_BG)
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", font=FONT, background=C_CARD, foreground=C_TEXT)
    st.configure("Bg.TFrame", background=C_BG)
    st.configure("TFrame", background=C_CARD)
    st.configure("TLabel", background=C_CARD, foreground=C_TEXT)
    st.configure("Muted.TLabel", foreground=C_MUTED)
    st.configure("Bold.TLabel", font=FONT_B)
    st.configure("TLabelframe", background=C_CARD, bordercolor="#d5dce7", relief="solid", borderwidth=1)
    st.configure("TLabelframe.Label", background=C_CARD, foreground=C_ACCENT, font=FONT_B)
    st.configure("Outer.TLabelframe", background=C_BG)
    st.configure("Conn.TLabelframe", background=C_CARD, bordercolor="#d5dce7", relief="solid", borderwidth=1)
    st.configure("Conn.TLabelframe.Label", background=C_BG, foreground=C_ACCENT, font=FONT_B)
    st.configure("Outer.TLabelframe.Label", background=C_BG, foreground=C_ACCENT, font=FONT_B)
    st.configure("TButton", padding=(10, 4), background="#e8edf5", bordercolor="#c3ccda")
    st.map("TButton", background=[("active", "#dbe4f2"), ("disabled", "#f1f3f6")])
    st.configure("Accent.TButton", background=C_ACCENT, foreground="white", font=FONT_B,
                 padding=(14, 5), bordercolor=C_ACCENT)
    st.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9db3e0")],
           foreground=[("disabled", "#eef2ff")])
    st.configure("Danger.TButton", foreground=C_BAD, bordercolor="#e8b4b4")
    st.configure("Small.TButton", padding=(6, 1), font=("Segoe UI", 9))
    st.configure("TNotebook", background=C_BG, borderwidth=0, tabmargins=(2, 4, 2, 0))
    st.configure("TNotebook.Tab", padding=(12, 5), background="#dde4ee", foreground=C_MUTED)
    st.map("TNotebook.Tab", background=[("selected", C_CARD)], foreground=[("selected", C_ACCENT)],
           font=[("selected", FONT_B)])
    st.configure("TCheckbutton", background=C_CARD)
    st.configure("TRadiobutton", background=C_CARD, font=FONT_B)
    st.map("TRadiobutton", background=[("disabled", C_CARD), ("active", C_CARD)])
    st.map("TCheckbutton", background=[("disabled", C_CARD), ("active", C_CARD)])
    st.configure("TEntry", padding=3, fieldbackground="white")
    st.configure("TCombobox", padding=2)
    st.configure("Horizontal.TProgressbar", background=C_ACCENT, troughcolor="#1e293b", bordercolor=C_HEAD)


def detect_router(cfg, log):
    """Что сейчас на роутере: 'openwrt' (наша прошивка), 'stock' (заводская) или ошибка."""
    if paramiko is None:
        raise StepError("Не установлен paramiko: pip install paramiko")
    router = cfg["router"]
    log(f"Проверка роутера {router}")
    if not wait_ping(router, True, 15):
        raise StepError(f"Роутер {router} не отвечает. Проверьте, что кабель от ноутбука воткнут в LAN-порт "
                        "роутера, а роутер включён и загрузился (1–2 минуты).")
    for user, pw in (("root", cfg.get("password") or ""), ("root", ""), (cfg["user"], cfg["password"]),
                     ("Admin", "Admin")):
        try:
            conn = Conn(router, user, [pw])
        except StepError:
            continue
        try:
            rel = conn.run(". /etc/openwrt_release 2>/dev/null; echo \"$DISTRIB_ID|$DISTRIB_RELEASE\"")[1].strip()
            apk = conn.run("command -v apk >/dev/null")[0] == 0
        finally:
            conn.close()
        rid, _, ver = rel.partition("|")
        major = re.match(r"(\d+)", ver)
        if rid == "OpenWrt" and (apk or (major and int(major.group(1)) >= 23)):
            log(f"  на роутере OpenWrt {ver} (вход: {user})")
            return {"state": "openwrt", "user": user, "password": pw, "version": ver}
        log(f"  заводская прошивка (вход: {user})")
        return {"state": "stock", "user": user, "password": pw, "version": ver}
    raise StepError("Роутер отвечает, но войти по SSH не получилось. Если вы меняли пароль root, впишите его "
                    "в поле «Пароль» в главном окне и повторите.")


WIZARD_STEPS = ("hello", "detect", "backup", "flash", "wan", "wifi", "rootpw", "components", "key", "mode", "check")
WIZARD_TITLES = {
    "hello": "Добро пожаловать", "detect": "Проверка роутера", "backup": "Бэкап заводской прошивки",
    "flash": "Установка OpenWrt", "wan": "Интернет от провайдера", "wifi": "Wi-Fi",
    "rootpw": "Пароль роутера", "components": "Что установить на роутер", "key": "Ключ VPN", "mode": "Режим обхода и DNS", "check": "Проверка",
}


class Wizard:
    """Мастер первой настройки: шаги по порядку, на каждом можно «Пропустить»."""

    def __init__(self, app):
        self.app = app
        self.state = None            # 'openwrt' / 'stock' после проверки
        self.i = 0
        w = tk.Toplevel(app.root)
        self.win = w
        w.title("Мастер настройки")
        w.configure(bg=C_CARD)
        w.geometry("720x560")
        w.minsize(640, 500)
        w.transient(app.root)
        head = tk.Frame(w, bg=C_HEAD)
        head.pack(fill="x")
        self.h_step = tk.Label(head, bg=C_HEAD, fg="#94a3b8", font=("Segoe UI", 10))
        self.h_step.pack(anchor="w", padx=16, pady=(10, 0))
        self.h_title = tk.Label(head, bg=C_HEAD, fg="white", font=("Segoe UI", 15, "bold"))
        self.h_title.pack(anchor="w", padx=16, pady=(0, 10))
        self.dots = tk.Frame(head, bg=C_HEAD)
        self.dots.pack(anchor="w", padx=16, pady=(0, 10))
        self.body = ttk.Frame(w, padding=16)
        self.body.pack(fill="both", expand=True)
        self.status = tk.Label(w, bg=C_CARD, fg=C_MUTED, font=FONT, anchor="w", justify="left", wraplength=680)
        self.status.pack(fill="x", padx=16)
        nav = ttk.Frame(w, padding=(16, 8, 16, 14))
        nav.pack(fill="x")
        self.b_back = ttk.Button(nav, text="← Назад", command=self.back)
        self.b_back.pack(side="left")
        self.b_close = ttk.Button(nav, text="Закрыть мастер", command=self.close)
        self.b_close.pack(side="left", padx=8)
        self.b_next = ttk.Button(nav, text="Далее →", style="Accent.TButton", command=self.next)
        self.b_next.pack(side="right")
        self.b_skip = ttk.Button(nav, text="Пропустить", command=self.skip)
        self.b_skip.pack(side="right", padx=8)
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.show()

    # ---------- навигация
    def steps(self):
        if self.state == "openwrt" or PANEL:
            return [s for s in WIZARD_STEPS if s not in ("backup", "flash")]
        return list(WIZARD_STEPS)

    def cur(self):
        st = self.steps()
        self.i = max(0, min(self.i, len(st) - 1))
        return st[self.i]

    def go(self, step):
        self.i = self.steps().index(step)
        self.show()

    def next(self):
        act = getattr(self, "act_" + self.cur(), None)
        if act:
            act()
        else:
            self.advance()

    def advance(self):
        if self.i >= len(self.steps()) - 1:
            return self.close(done=True)
        self.i += 1
        self.show()

    def skip(self):
        self.advance()

    def back(self):
        if self.i > 0:
            self.i -= 1
            self.show()

    def close(self, done=False):
        self.app.wizard_done = True
        self.app.save_settings()
        self.app.wizard = None
        self.win.destroy()
        if done:
            self.app.log("Мастер настройки завершён. Всё можно поменять в разделах главного окна.")

    def say(self, text, color=None):
        self.status.configure(text=text, fg=color or C_MUTED)

    def run(self, fn, cfg, on_ok, busy_text):
        """Запуск действия программы в фоне; по успеху — on_ok(результат)."""
        if self.app.busy:
            self.say("Подождите: программа ещё выполняет прошлое действие (смотрите журнал).", C_WARN)
            return
        for b in (self.b_next, self.b_skip, self.b_back):
            b.configure(state="disabled")
        self.say(busy_text + " Ход работы — в журнале главного окна.", C_ACCENT)

        def done(res):
            if self.win.winfo_exists():
                on_ok(res)
        self.app.run_bg(fn, cfg, None, on_done=done)

        def watch():
            if not self.win.winfo_exists():
                return
            if self.app.busy:
                self.win.after(400, watch)
                return
            for b in (self.b_next, self.b_skip, self.b_back):
                b.configure(state="normal")
            if self.app.last_ok is False:
                self.say("Не получилось — причина в журнале и во всплывающем окне. Можно исправить и нажать "
                         "«Далее» ещё раз или «Пропустить».", C_BAD)
        self.win.after(400, watch)

    # ---------- отрисовка шагов
    def clear(self):
        for wdg in self.body.winfo_children():
            wdg.destroy()

    def text(self, t, **kw):
        lbl = ttk.Label(self.body, text=t, wraplength=660, justify="left", **kw)
        lbl.pack(anchor="w", pady=(0, 8))
        return lbl

    def row(self, label, widget_factory):
        f = ttk.Frame(self.body)
        f.pack(anchor="w", pady=3, fill="x")
        ttk.Label(f, text=label, width=22).pack(side="left")
        w = widget_factory(f)
        w.pack(side="left")
        return w

    def entry(self, parent, value="", show=None, width=30):
        e = ttk.Entry(parent, width=width, show=show or "")
        e.insert(0, value)
        return e

    def show(self):
        st = self.steps()
        step = self.cur()
        self.h_step.configure(text=f"Шаг {self.i + 1} из {len(st)}")
        self.h_title.configure(text=WIZARD_TITLES[step])
        for d in self.dots.winfo_children():
            d.destroy()
        for k in range(len(st)):
            tk.Label(self.dots, text="●", bg=C_HEAD, font=("Segoe UI", 9),
                     fg=C_ACCENT if k == self.i else ("#4ade80" if k < self.i else "#475569")).pack(side="left")
        self.clear()
        self.say("")
        self.b_back.configure(state="normal" if self.i > 0 else "disabled")
        self.b_skip.configure(state="normal" if step not in ("hello",) else "disabled")
        self.b_next.configure(text="Готово" if step == "check" else "Далее →")
        getattr(self, "page_" + step)()

    # 0. приветствие
    def page_hello(self):
        self.text(("Мастер шаг за шагом настроит роутер на OpenWrt: интернет, Wi-Fi, пароль, VPN и обход "
                   "блокировок. Любой шаг можно пропустить и сделать потом в разделах главного окна."
                   if PANEL else
                   "Мастер шаг за шагом настроит роутер SNR-CPE-AX2: прошивка OpenWrt, интернет, Wi-Fi, "
                   "пароль, VPN и обход блокировок. Любой шаг можно пропустить и сделать потом в разделах "
                   "главного окна."), style="Bold.TLabel")
        self.text("Перед началом:\n"
                  + ("  1. Компьютер подключён к роутеру — кабелем в LAN-порт или по Wi-Fi.\n" if PANEL else
                     "  1. Кабель от ноутбука — в LAN-порт роутера (любой жёлтый/серый, не WAN).\n")
                  + "  2. Кабель провайдера — в порт WAN (интернет понадобится для VPN и zapret).\n"
                  "  3. Роутер включён и загрузился (лампочки перестали мигать хаотично)."
                  + ("" if PANEL else
                     "\n  4. Если будете прошивать — ноутбук подключён к зарядке, а роутер к питанию надёжно."))
        self.text("Нажмите «Далее».", style="Muted.TLabel")

    # 1. проверка роутера
    def page_detect(self):
        self.text("Программа подключится к роутеру и определит, что на нём: заводская прошивка или уже OpenWrt. "
                  "Если OpenWrt уже стоит, шаги бэкапа и прошивки будут пропущены.")
        self.det_ip = self.row("IP роутера", lambda p: self.entry(p, self.app.router.get(), width=18))
        if self.state:
            self.say("Определено: " + ("OpenWrt" if self.state == "openwrt" else "заводская прошивка") +
                     ". Нажмите «Далее», чтобы проверить ещё раз.", C_OK)

    def act_detect(self):
        ip = self.det_ip.get().strip() or "192.168.1.1"
        self.app.router.delete(0, "end")
        self.app.router.insert(0, ip)
        cfg = self.app._router_cfg()

        def ok(res):
            if PANEL and res["state"] != "openwrt":
                self.say("На роутере не OpenWrt. Панель управления работает только с OpenWrt — сначала прошейте "
                         "его (SNR-CPE-AX2 — программой OpenWrt Installer, другие модели — по инструкции OpenWrt).",
                         C_BAD)
                return
            self.state = res["state"]
            self.app.set_login(res["user"], res["password"])
            self.app.log("Мастер: " + ("на роутере уже OpenWrt — бэкап и прошивку пропускаю"
                                       if self.state == "openwrt" else "на роутере заводская прошивка"))
            self.go("wan" if self.state == "openwrt" else "backup")
        self.run(detect_router, cfg, ok, "Проверяю роутер…")

    # 2. бэкап
    def page_backup(self):
        self.text("Бэкап сохраняет заводскую прошивку и калибровку Wi-Fi (Factory) в папку данных программы "
                  "(кнопка «Папка данных» на вкладке «Роутер»). С ним роутер можно вернуть «как из магазина». Займёт 5–15 минут.")
        self.text("Без бэкапа вернуть заводскую прошивку будет нельзя. Пропускайте, только если он уже есть.",
                  style="Muted.TLabel")
        self.bk_adapter = self.row("Сетевой адаптер", lambda p: ttk.Combobox(
            p, width=34, state="readonly", values=self.app.adapter["values"]))
        self.bk_adapter.set(self.app.adapter.get())

    def _sync_adapter(self, combo):
        if combo.get():
            self.app.adapter.set(combo.get())

    def act_backup(self):
        self._sync_adapter(self.bk_adapter)
        cfg = dict(adapter=self.app.adapter.get(), router=self.app.router.get().strip(),
                   user=self.app.user.get().strip(), password=self.app.pw.get(), quick=False)
        if not cfg["adapter"]:
            self.say("Выберите сетевой адаптер — тот, в который воткнут кабель роутера.", C_BAD)
            return
        self.run(backup_mtd, cfg, lambda _: self.advance(), "Делаю бэкап…")

    # 3. прошивка
    def page_flash(self):
        files = find_files()
        miss = [k for k, v in files.items() if not v]
        self.text("Программа установит OpenWrt: загрузчик, затем recovery по TFTP, затем основную прошивку. "
                  "Это 10–15 минут. НЕ выключайте роутер и ноутбук, не трогайте кабель!")
        self.text(f"IP ноутбука на время прошивки станет {PC_IP}. После прошивки логин в программе сам "
                  "сменится на root.", style="Muted.TLabel")
        self.fl_adapter = self.row("Сетевой адаптер", lambda p: ttk.Combobox(
            p, width=34, state="readonly", values=self.app.adapter["values"]))
        self.fl_adapter.set(self.app.adapter.get())
        if miss:
            self.say("Не найдены файлы прошивки: " + ", ".join(miss) + ". Эта сборка без встроенных прошивок — положите "
                     "их в папку firmware рядом с программой.", C_BAD)
        else:
            self.say("Файлы прошивки найдены ✔", C_OK)

    def act_flash(self):
        self._sync_adapter(self.fl_adapter)
        if not self.app.adapter.get():
            self.say("Выберите сетевой адаптер.", C_BAD)
            return
        if not messagebox.askokcancel("Прошивка", WARNING_TEXT, icon="warning", parent=self.win):
            return
        cfg = dict(adapter=self.app.adapter.get(), router=self.app.router.get().strip(),
                   user=self.app.user.get().strip(), password=self.app.pw.get())

        def ok(_):
            self.app.set_openwrt_login()
            self.state = "openwrt"
            self.app.restore_dhcp()
            self.say("Прошивка завершена. Подождите 3–5 минут, пока роутер загрузится, затем нажмите «Далее».",
                     C_OK)
            self.go("wan")
        self.run(install, cfg, ok, "Прошиваю роутер… не выключайте питание!")

    # 4. интернет
    def page_wan(self):
        self.text("Как провайдер даёт интернет? Если в договоре есть логин и пароль — это PPPoE. "
                  "Если ничего не указано — обычно DHCP (автоматически).")
        self.wan = self.row("Тип подключения", lambda p: ttk.Combobox(
            p, width=26, state="readonly", values=["DHCP (автоматически)", "PPPoE (логин/пароль)"]))
        cur = self.app.wan_type.get()
        self.wan.set(cur if cur in self.wan["values"] else "DHCP (автоматически)")
        self.ppp_u = self.row("Логин PPPoE", lambda p: self.entry(p, self.app.ppp_user.get()))
        self.ppp_p = self.row("Пароль PPPoE", lambda p: self.entry(p, self.app.ppp_pass.get(), show="•"))

    def act_wan(self):
        a = self.app
        a.wan_type.set(self.wan.get())
        for src, dst in ((self.ppp_u, a.ppp_user), (self.ppp_p, a.ppp_pass)):
            dst.delete(0, "end")
            dst.insert(0, src.get())
        cfg = a._router_cfg()
        cfg.update(wan_type="pppoe" if "PPPoE" in self.wan.get() else "dhcp", ppp_user=self.ppp_u.get().strip(),
                   ppp_pass=self.ppp_p.get(), ssid="", wifi_key="", country="RU")
        self.run(net_setup, cfg, lambda _: self.advance(), "Настраиваю интернет…")

    # 5. Wi-Fi
    def page_wifi(self):
        self.text("Придумайте имя сети и пароль (не короче 8 символов).")
        self.w_ssid = self.row("Имя Wi-Fi", lambda p: self.entry(p, self.app.ssid.get()))
        self.w_key = self.row("Пароль Wi-Fi", lambda p: self.entry(p, self.app.wifi_key.get()))
        self.w_split = tk.BooleanVar(value=self.app.wifi_split.get())
        ttk.Checkbutton(self.body, text="Разные имена для 2,4 и 5 ГГц (к 5 ГГц добавится _5G)",
                        variable=self.w_split).pack(anchor="w", pady=(6, 0))
        self.text("После применения ноутбук по Wi-Fi может отключиться — это нормально, кабель продолжит работать.",
                  style="Muted.TLabel")

    def act_wifi(self):
        a = self.app
        for src, dst in ((self.w_ssid, a.ssid), (self.w_key, a.wifi_key)):
            dst.delete(0, "end")
            dst.insert(0, src.get())
        a.wifi_split.set(self.w_split.get())
        cfg = a._router_cfg()
        cfg.update(wan_type="skip", ppp_user="", ppp_pass="", ssid=self.w_ssid.get().strip(),
                   wifi_key=self.w_key.get(), country=a.country.get() or "RU", wifi_split=self.w_split.get())
        self.run(net_setup, cfg, lambda _: self.advance(), "Настраиваю Wi-Fi…")

    # 6. пароль root
    def page_rootpw(self):
        self.text("Сейчас вход в роутер без пароля — любой, кто подключится к Wi-Fi, сможет зайти в настройки. "
                  "Задайте пароль (минимум 6 символов). Он же будет паролем входа в веб-панель OpenWrt.")
        self.r_pw = self.row("Новый пароль", lambda p: self.entry(p, show="•"))
        self.r_pw2 = self.row("Ещё раз", lambda p: self.entry(p, show="•"))
        self.text("Запомните или запишите пароль. Программа сохранит его у себя в зашифрованном виде.",
                  style="Muted.TLabel")

    def act_rootpw(self):
        pw = self.r_pw.get()
        if pw != self.r_pw2.get():
            self.say("Пароли не совпадают.", C_BAD)
            return
        if len(pw) < 6:
            self.say("Пароль слишком короткий (минимум 6 символов).", C_BAD)
            return
        cfg = self.app._router_cfg()
        cfg["new_pw"] = pw

        def ok(_):
            self.app.set_login("root", pw)
            self.advance()
        self.run(set_root_password, cfg, ok, "Меняю пароль…")

    # 6½. компоненты
    def page_components(self):
        self.text("Программа проверит, что уже стоит на роутере. Выберите, чем обходить блокировки — недостающее "
                  "установится. Удалить или поставить что-то потом можно в разделе «Роутер и компоненты».")
        self.c_info = ttk.Label(self.body, style="Muted.TLabel", text="Проверяю роутер…", justify="left",
                                wraplength=660)
        self.c_info.pack(anchor="w", pady=(0, 6))
        res = getattr(self, "comp_res", None)
        have = res["items"] if res else {}
        eng = "forkop" if have.get("forkop", (False,))[0] else "xray"
        if self.app.engine.get() == "forkop":
            eng = "forkop"
        self.c_engine = tk.StringVar(value=eng)
        ttk.Label(self.body, text="VPN:", style="Bold.TLabel").pack(anchor="w")
        for val, txt in (("xray", "Встроенный VPN (xray) — всё, кроме РФ, через VPN; подписки и автопереключение"),
                         ("forkop", "Forkop (sing-box) — правила по сайтам, своя веб-панель, zapret внутри"),
                         ("none", "Без VPN")):
            ttk.Radiobutton(self.body, text=txt, variable=self.c_engine, value=val).pack(anchor="w", padx=(12, 0))
        self.c_zapret = tk.BooleanVar(value=bool(have.get("zapret", (False,))[0]))
        self.c_doh = tk.BooleanVar(value=bool(have.get("doh", (False,))[0]))
        ttk.Label(self.body, text="Дополнительно:", style="Bold.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(self.body, text="Zapret — YouTube и Discord без VPN (работает не у всех провайдеров)",
                        variable=self.c_zapret).pack(anchor="w", padx=(12, 0))
        ttk.Checkbutton(self.body, text="Зашифрованный DNS — провайдер не сможет подменять адреса сайтов",
                        variable=self.c_doh).pack(anchor="w", padx=(12, 0))
        if res:
            self._show_comp_info(res)
        else:
            self.win.after(100, self.check_components)

    def _show_comp_info(self, res):
        parts = []
        for key, title in COMPONENTS:
            inst, run, _ = res["items"][key]
            parts.append(f"{title}: " + (("работает" if run else "установлен") if inst else "нет"))
        self.c_info.configure(text="Сейчас на роутере — " + "; ".join(parts) + "\n" + res.get("free", ""))

    def check_components(self):
        def ok(res):
            self.comp_res = res
            self.app.show_components(res)
            if self.cur() == "components":
                self.show()
        self.run(components_status, self.app._router_cfg(), ok, "Проверяю, что установлено…")

    def act_components(self):
        eng = self.c_engine.get()
        self.app.engine.set("forkop" if eng == "forkop" else "builtin")
        self.app.vpn_choice.set({"xray": "builtin", "forkop": "forkop"}.get(eng, "off"))
        self.app.zapret_on.set(self.c_zapret.get())
        self.app.update_mode_desc()
        want = {"engine": eng, "zapret": self.c_zapret.get(), "doh": self.c_doh.get()}
        if want["doh"] and self.app.dns_mode.get() == "isp":
            self.app.dns_mode.set("auto")
        cfg = self.app._router_cfg()
        cfg.update(want=want, have=(getattr(self, "comp_res", None) or {}).get("items", {}),
                   engine=self.app.engine.get())

        def ok(_):
            self.comp_res = None
            self.advance()
        self.run(install_components, cfg, ok, "Устанавливаю выбранное (до 10 минут)…")

    # 7. ключ
    def page_key(self):
        self.text("Вставьте ключ VPN (ссылка vless://…) или ссылку на подписку (https://…). Нет ключа — "
                  "нажмите «Пропустить»: заработает только zapret (YouTube и Discord).")
        self.k_link = self.row("vless-ссылка", lambda p: self.entry(p, self.app.vless.get(), width=52))
        self.k_sub = self.row("или подписка", lambda p: self.entry(p, self.app.sub.get(), width=52))
        bf = ttk.Frame(self.body)
        bf.pack(anchor="w", pady=(8, 0))
        ttk.Button(bf, text="Принять с телефона", command=self.app.receive_from_phone).pack(side="left")
        ttk.Button(bf, text="Сканировать QR камерой", command=self.app.scan_qr_camera).pack(side="left", padx=8)
        ttk.Button(bf, text="Обновить поля", style="Small.TButton", command=self.show).pack(side="left")
        self.text("Ссылку, полученную с телефона или камеры, программа впишет в главное окно — нажмите «Обновить поля».",
                  style="Muted.TLabel")

    def act_key(self):
        a = self.app
        link, sub = self.k_link.get().strip(), self.k_sub.get().strip()
        if sub and sub != a.sub.get().strip():
            a.sub.delete(0, "end")
            a.sub.insert(0, sub)
        if link:
            try:
                link_kind(link)
            except StepError as e:
                self.say(str(e), C_BAD)
                return
            if is_sub_url(link):
                a.sub.delete(0, "end")
                a.sub.insert(0, link)
                link = ""
            else:
                a.vless.delete(0, "end")
                a.vless.insert(0, link)
        if not link and is_sub_url(a.sub.get()):
            if not a.vless.get().strip():
                self.say("Загружаю серверы из подписки — выберите сервер в открывшемся окне, затем снова «Далее».",
                         C_ACCENT)
                a.load_subscription()
                return
        a.save_settings()
        self.advance()

    # 8. режим и DNS
    def page_mode(self):
        a = self.app
        has_key = bool(a.vless.get().strip() or is_sub_url(a.sub.get()))
        self.text("Как обходить блокировки:", style="Bold.TLabel")
        for val, txt in (("builtin", "VPN: встроенный (xray)"), ("forkop", "VPN: Forkop (sing-box)"),
                         ("off", "Без VPN")):
            ttk.Radiobutton(self.body, text=txt, variable=a.vpn_choice, value=val, command=a.update_mode_desc,
                            state="normal" if (has_key or val == "off") else "disabled").pack(anchor="w")
        if not has_key:
            a.vpn_choice.set("off")
        ttk.Checkbutton(self.body, text="Zapret для YouTube и Discord", variable=a.zapret_on,
                        command=a.update_mode_desc).pack(anchor="w", pady=(6, 0))
        self.text("Без ключа VPN доступен только zapret. Самый надёжный вариант — VPN.", style="Muted.TLabel")
        self.m_dns = self.row("DNS", lambda p: ttk.Combobox(p, width=52, state="readonly",
                                                              values=[DNS_MODES[k] for k in DNS_MODES]))
        self.m_dns.set(DNS_MODES.get(a.dns_mode.get(), DNS_MODES["auto"]))
        self.text("Применение займёт несколько минут: роутер скачает и установит нужные пакеты.",
                  style="Muted.TLabel")

    def act_mode(self):
        a = self.app
        mode = a.derive_mode()
        a.update_mode_desc()
        rev = {v: k for k, v in DNS_MODES.items()}
        a.dns_mode.set(rev.get(self.m_dns.get(), "auto"))
        cfg = a._router_cfg()
        cfg["mode"] = mode
        cfg["autotune"] = a.autotune.get()
        self.run(apply_mode, cfg, lambda _: self.advance(), "Применяю…")

    # 9. проверка
    def page_check(self):
        self.text("Финальная проверка: программа проверит интернет, VPN, YouTube, Telegram и DNS прямо с роутера.")
        self.c_grid = ttk.Frame(self.body)
        self.c_grid.pack(anchor="w", fill="x", pady=(6, 0))
        ttk.Button(self.body, text="Проверить сейчас", command=self.do_check).pack(anchor="w", pady=(10, 0))

    def do_check(self):
        def ok(rows):
            for wdg in self.c_grid.winfo_children():
                wdg.destroy()
            for i, (name, text, good) in enumerate(rows):
                col = C_TEXT if good is None else (C_OK if good else C_BAD)
                tk.Label(self.c_grid, text="●" if good is not None else "○", bg=C_CARD,
                         fg=col if good is not None else "#94a3b8").grid(row=i, column=0, sticky="w")
                ttk.Label(self.c_grid, text=name, style="Bold.TLabel").grid(row=i, column=1, sticky="w", padx=6)
                tk.Label(self.c_grid, text=text, fg=col, bg=C_CARD, font=FONT).grid(row=i, column=2, sticky="w")
            self.app.show_status(rows)
            bad = sum(1 for r in rows if r[2] is False)
            self.say("Всё в порядке! Нажмите «Готово»." if not bad else
                     f"Есть проблемы ({bad}) — красные пункты. Их можно поправить в разделах главного окна.",
                     C_OK if not bad else C_WARN)
        self.run(router_status, self.app._router_cfg(), ok, "Проверяю…")

    def act_check(self):
        self.close(done=True)


class ScrollPage:
    """Страница с вертикальной прокруткой (колесо мыши работает, когда курсор над страницей)."""

    def __init__(self, parent):
        self.outer = tk.Frame(parent, bg=C_CARD)
        self.canvas = tk.Canvas(self.outer, bg=C_CARD, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=(16, 12, 16, 12))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._bind_wheel)
            w.bind("<Leave>", self._unbind_wheel)

    def _wheel(self, e):
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if getattr(e, "num", 0) == 4:
            step = -1
        elif getattr(e, "num", 0) == 5:
            step = 1
        else:
            step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")

    def _bind_wheel(self, _e):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", self._wheel)
        self.canvas.bind_all("<Button-5>", self._wheel)

    def _unbind_wheel(self, _e):
        x, y = self.canvas.winfo_pointerxy()
        w = self.canvas.winfo_containing(x, y)
        while w is not None and w is not self.outer:
            w = w.master
        if w is None:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(ev)


class Collapsible(ttk.Frame):
    """Сворачиваемый блок: заголовок-кнопка «▸ …», по щелчку раскрывает содержимое."""

    def __init__(self, parent, title, opened=False):
        super().__init__(parent)
        self.title = title
        self.head = tk.Label(self, text="", bg="#eef2f7", fg=C_ACCENT, font=FONT_B, anchor="w", padx=10, pady=7,
                             cursor="hand2")
        self.head.pack(fill="x")
        self.head.bind("<Button-1>", lambda e: self.toggle())
        self.body = ttk.Frame(self, padding=(12, 8, 8, 4))
        self.opened = not opened
        self.toggle()

    def toggle(self):
        self.opened = not self.opened
        self.head.configure(text=("▾  " if self.opened else "▸  ") + self.title)
        if self.opened:
            self.body.pack(fill="x")
        else:
            self.body.pack_forget()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} v{APP_VERSION}")
        try:
            self._icons = ([tk.PhotoImage(data=PANEL_ICON_64), tk.PhotoImage(data=PANEL_ICON_32)] if PANEL
                           else [tk.PhotoImage(data=ICON_64), tk.PhotoImage(data=ICON_32)])
            root.iconphoto(True, *self._icons)
        except tk.TclError:
            pass
        root.geometry("1080x820")
        root.minsize(900, 600)
        setup_style(root)
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.busy = False
        self.sub_servers = []          # все vless-ключи из последней загруженной подписки
        self.excluded = []             # имена серверов, которые роутер никогда не использует
        self.wizard = None
        self.wizard_done = False
        self.last_ok = None            # результат последнего фонового действия (для мастера)
        self.secret_vars = []

        # ---------- шапка
        head = tk.Frame(root, bg=C_HEAD)
        head.pack(fill="x")
        tk.Label(head, text="Панель управления" if PANEL else "OpenWrt Installer", bg=C_HEAD, fg="white",
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=(16, 8), pady=10)
        tk.Label(head, text=(f"VPN utility · v{APP_VERSION}" if PANEL else f"SNR-CPE-AX2 · v{APP_VERSION}"),
                 bg=C_HEAD, fg="#94a3b8", font=("Segoe UI", 10)).pack(side="left", pady=(14, 8))
        tk.Button(head, text="✨ Мастер настройки", command=self.open_wizard, bg=C_ACCENT, fg="white",
                  activebackground="#1d4ed8", activeforeground="white", relief="flat", bd=0, padx=12, pady=4,
                  font=FONT_B, cursor="hand2").pack(side="right", padx=(0, 16))
        self.head_state = tk.Label(head, text="● состояние не проверено", bg=C_HEAD, fg="#94a3b8", font=FONT_B)
        self.head_state.pack(side="right", padx=16)
        self.progress = ttk.Progressbar(head, mode="indeterminate", length=120)

        # ---------- подключение (одна строка)
        cf = tk.Frame(root, bg=C_CARD, highlightthickness=1, highlightbackground="#d5dce7")
        cf.pack(fill="x", padx=12, pady=(8, 0))
        cin = ttk.Frame(cf, padding=(10, 6))
        cin.pack(fill="x")
        ttk.Label(cin, text="Роутер", style="Bold.TLabel").pack(side="left")
        self.router = ttk.Entry(cin, width=14)
        self.router.insert(0, "192.168.1.1")
        self.router.pack(side="left", padx=(6, 12))
        ttk.Label(cin, text="Логин").pack(side="left")
        self.user = ttk.Entry(cin, width=9)
        self.user.insert(0, "root" if PANEL else "Admin")
        self.user.pack(side="left", padx=(6, 12))
        ttk.Label(cin, text="Пароль").pack(side="left")
        self.pw = ttk.Entry(cin, width=13, show="•")
        self.pw.insert(0, "" if PANEL else "Admin")
        self.pw.pack(side="left", padx=(6, 10))
        if not PANEL:
            ttk.Button(cin, text="OpenWrt: root", style="Small.TButton",
                       command=lambda: self.set_login("root", "")).pack(side="left", padx=2)
            ttk.Button(cin, text="Заводская: Admin", style="Small.TButton",
                       command=lambda: self.set_login("Admin", "Admin")).pack(side="left", padx=2)
        self.login_hint = ttk.Label(cin, style="Muted.TLabel", text="")
        self.login_hint.pack(side="left", padx=(12, 0))

        # ---------- боковое меню + страницы
        main = ttk.Frame(root, style="Bg.TFrame")
        main.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        side = tk.Frame(main, bg=C_HEAD, width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.pages_box = tk.Frame(main, bg=C_CARD, highlightthickness=1, highlightbackground="#d5dce7")
        self.pages_box.pack(side="left", fill="both", expand=True)
        self.pages, self.nav_btns = {}, {}
        nav = [("home", "Главная"), ("bypass", "Обход блокировок"), ("net", "Интернет, Wi-Fi, DNS"),
               ("router", "Роутер и компоненты")] + ([] if PANEL else [("flash", "Прошивка и бэкап")])
        tk.Label(side, text="МЕНЮ", bg=C_HEAD, fg="#64748b", font=("Segoe UI", 8, "bold")).pack(anchor="w",
                                                                                              padx=16, pady=(12, 4))
        for key, title in nav:
            b = tk.Label(side, text=title, bg=C_HEAD, fg="#cbd5e1", font=("Segoe UI", 11), anchor="w",
                         padx=16, pady=9, cursor="hand2")
            b.pack(fill="x")
            b.bind("<Button-1>", lambda e, k=key: self.show_page(k))
            b.bind("<Enter>", lambda e, w=b, k=key: w.configure(bg="#1e293b") if self.cur_page != k else None)
            b.bind("<Leave>", lambda e, w=b, k=key: w.configure(bg=C_HEAD) if self.cur_page != k else None)
            self.nav_btns[key] = b
            self.pages[key] = ScrollPage(self.pages_box)
        self.cur_page = None
        P = {k: v.inner for k, v in self.pages.items()}
        if PANEL:
            P["flash"] = ttk.Frame(self.pages_box)      # прошивки нет — виджеты создаются, но не показываются

        def card(parent, title, pady=(0, 10)):
            f = ttk.LabelFrame(parent, text=title, padding=10)
            f.pack(fill="x", pady=pady)
            return f

        def muted(parent, text, **kw):
            lbl = ttk.Label(parent, style="Muted.TLabel", wraplength=760, justify="left", text=text)
            lbl.pack(anchor="w", **kw)
            return lbl

        # ===== Главная
        c = card(P["home"], "Состояние роутера")
        top = ttk.Frame(c)
        top.pack(fill="x")
        ttk.Button(top, text="Обновить состояние", style="Accent.TButton", command=self.start_status).pack(side="left")
        ttk.Button(top, text="Подробная диагностика", command=self.diag_vpn).pack(side="left", padx=8)
        self.st_grid = ttk.Frame(c)
        self.st_grid.pack(fill="x", pady=(10, 0))
        ttk.Label(self.st_grid, style="Muted.TLabel", wraplength=760, justify="left",
                  text="Нажмите «Обновить состояние»: программа проверит интернет, VPN-сервер, zapret, YouTube, "
                       "Telegram и DNS прямо с роутера.").grid(row=0, column=0, sticky="w")
        c = card(P["home"], "С чего начать")
        muted(c, "Первый раз? Нажмите «Мастер настройки» вверху справа — он проведёт по шагам: проверка роутера, "
                 + ("" if PANEL else "прошивка, ") + "интернет, Wi-Fi, пароль, что установить, ключ VPN и способ "
                 "обхода блокировок. Потом любую настройку можно поменять в разделах слева.")

        # ===== Обход блокировок
        c = card(P["bypass"], "Как обходить блокировки")
        self.vpn_choice = tk.StringVar(value="builtin")
        self.zapret_on = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="vpn")          # производные (для мастера и сохранения)
        self.engine = tk.StringVar(value="builtin")
        ttk.Label(c, text="VPN", style="Bold.TLabel").pack(anchor="w")
        vr = ttk.Frame(c)
        vr.pack(anchor="w", pady=(2, 6))
        for val, txt in (("builtin", "Встроенный (xray)"), ("forkop", "Forkop (sing-box)"), ("off", "Выключен")):
            ttk.Radiobutton(vr, text=txt, variable=self.vpn_choice, value=val,
                            command=self.update_mode_desc).pack(side="left", padx=(0, 18))
        zr = ttk.Frame(c)
        zr.pack(anchor="w")
        ttk.Checkbutton(zr, text="Zapret для YouTube и Discord (без VPN-сервера)", variable=self.zapret_on,
                        command=self.update_mode_desc).pack(side="left")
        self.autotune = tk.BooleanVar(value=True)
        ttk.Checkbutton(zr, text="подобрать стратегию, если ещё не подбиралась", variable=self.autotune).pack(
            side="left", padx=(16, 0))
        self.mode_desc = muted(c, "", pady=(8, 0))
        br = ttk.Frame(c)
        br.pack(anchor="w", pady=(10, 0))
        ttk.Button(br, text="Применить", style="Accent.TButton", command=self.start_mode).pack(side="left")
        muted(br, "  уже установленное не переустанавливается — меняется только путь трафика").pack(side="left")

        c = card(P["bypass"], "Ключ VPN")
        c.columnconfigure(1, weight=1)
        ttk.Label(c, text="vless-ссылка").grid(row=0, column=0, sticky="w")
        self.vless = ttk.Entry(c, show="•")
        self.vless.grid(row=0, column=1, sticky="we", padx=6)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="показать", variable=self.show_key,
                        command=lambda: self.vless.configure(show="" if self.show_key.get() else "•")).grid(
            row=0, column=2, sticky="w")
        ttk.Label(c, text="Подписка").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sub = ttk.Entry(c)
        self.sub.grid(row=1, column=1, sticky="we", padx=6, pady=(6, 0))
        ttk.Button(c, text="Загрузить серверы", command=self.load_subscription).grid(row=1, column=2, sticky="w",
                                                                                   pady=(6, 0))
        kb = ttk.Frame(c)
        kb.grid(row=2, column=1, columnspan=2, sticky="w", padx=6, pady=(8, 0))
        ttk.Button(kb, text="Принять с телефона", command=self.receive_from_phone).pack(side="left")
        ttk.Button(kb, text="QR камерой", command=self.scan_qr_camera).pack(side="left", padx=6)
        ttk.Button(kb, text="Удалить ключ и подписку", style="Danger.TButton", command=self.forget_keys).pack(
            side="left")
        self.sub_info = ttk.Label(kb, style="Muted.TLabel", text="")
        self.sub_info.pack(side="left", padx=8)

        c = Collapsible(P["bypass"], "Настройки VPN: серверы, исключения, базы, Forkop")
        c.pack(fill="x", pady=(0, 10))
        b = c.body
        self.keep_all = tk.BooleanVar(value=True)
        ttk.Checkbutton(b, text="Хранить все серверы подписки на роутере — если текущий упадёт, роутер сам "
                        "переключится", variable=self.keep_all).pack(anchor="w")
        r = ttk.Frame(b)
        r.pack(anchor="w", pady=(2, 0))
        self.auto_sub = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Роутер сам обновляет подписку каждые 6 часов", variable=self.auto_sub).pack(
            side="left")
        ttk.Button(r, text="Обновить сейчас", style="Small.TButton", command=self.start_sub_update).pack(
            side="left", padx=10)
        self.lat_switch = tk.BooleanVar(value=True)
        ttk.Checkbutton(b, text="Уходить с медленного сервера на заметно более быстрый (раз в 15 минут)",
                        variable=self.lat_switch).pack(anchor="w", pady=(2, 0))
        ex = ttk.Frame(b)
        ex.pack(fill="x", pady=(8, 0))
        ex.columnconfigure(0, weight=1)
        ex.columnconfigure(1, weight=1)
        ttk.Label(ex, text="Всегда напрямую (домены/IP)").grid(row=0, column=0, sticky="w")
        ttk.Label(ex, text="Всегда через VPN").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.user_direct = tk.Text(ex, width=36, height=2, font=FONT, relief="solid", bd=1)
        self.user_direct.grid(row=1, column=0, sticky="we")
        self.user_proxy = tk.Text(ex, width=36, height=2, font=FONT, relief="solid", bd=1)
        self.user_proxy.grid(row=1, column=1, sticky="we", padx=(12, 0))
        muted(b, "Встроенный VPN: напрямую — сайты и IP России, Google (кроме YouTube и Gemini), Apple, Microsoft, "
                 "торренты; остальное через VPN. Forkop: через VPN — списки «заблокировано в РФ», гео-блок, "
                 "нейросети; тонкие правила — в его панели.", pady=(8, 0))
        r = ttk.Frame(b)
        r.pack(anchor="w", pady=(8, 0))
        ttk.Button(r, text="Обновить geo-базы", command=self.start_geo).pack(side="left")
        ttk.Button(r, text="Панель Forkop", command=self.open_forkop).pack(side="left", padx=8)
        self.fk_ext = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="Forkop: sing-box extended (для ключей с XHTTP; ставится сам, если такой ключ найден)",
                        variable=self.fk_ext).pack(anchor="w", pady=(8, 0))
        self.fk_purge = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="При установке Forkop удалить встроенный xray и geo-базы (освободит место)",
                        variable=self.fk_purge).pack(anchor="w")

        c = Collapsible(P["bypass"], "Zapret: стратегия обхода")
        c.pack(fill="x", pady=(0, 10))
        b = c.body
        r = ttk.Frame(b)
        r.pack(anchor="w")
        ttk.Button(r, text="Подобрать автоматически", style="Accent.TButton", command=self.start_tune).pack(
            side="left")
        ttk.Button(r, text="Показать текущую", command=self.start_get_strategy).pack(side="left", padx=8)
        ttk.Button(r, text="Отключить zapret", command=self.stop_zapret).pack(side="left")
        muted(b, "Автоподбор перебирает стратегии под вашего провайдера 10–30 минут. Или вставьте свою — например "
                 "строку из blockcheck (можно вместе со словом nfqws):", pady=(8, 4))
        self.strategy = tk.Text(b, height=3, font=("Consolas", 10), relief="solid", bd=1, wrap="word")
        self.strategy.pack(fill="x")
        r = ttk.Frame(b)
        r.pack(anchor="w", pady=(6, 0))
        ttk.Button(r, text="Применить стратегию", command=self.start_set_strategy).pack(side="left")
        self.strat_wrap = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="только для YouTube и Discord", variable=self.strat_wrap).pack(side="left", padx=10)
        self.update_mode_desc()

        # ===== Интернет, Wi-Fi, DNS
        c = card(P["net"], "Интернет от провайдера (WAN)")
        ttk.Label(c, text="Тип подключения").grid(row=0, column=0, sticky="w")
        self.wan_type = ttk.Combobox(c, width=26, state="readonly",
                                     values=["Не менять", "DHCP (автоматически)", "PPPoE (логин/пароль)"])
        self.wan_type.current(0)
        self.wan_type.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(c, text="Логин PPPoE").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.ppp_user = ttk.Entry(c, width=26)
        self.ppp_user.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(c, text="Пароль PPPoE").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.ppp_pass = ttk.Entry(c, width=20, show="•")
        self.ppp_pass.grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))
        c = card(P["net"], "Wi-Fi")
        ttk.Label(c, text="Имя сети").grid(row=0, column=0, sticky="w")
        self.ssid = ttk.Entry(c, width=26)
        self.ssid.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(c, text="Пароль").grid(row=0, column=2, sticky="w")
        self.wifi_key = ttk.Entry(c, width=20, show="•")
        self.wifi_key.grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(c, text="Страна").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.country = ttk.Entry(c, width=6)
        self.country.insert(0, "RU")
        self.country.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        self.wifi_split = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Разные имена для 2,4 и 5 ГГц (к 5 ГГц добавится _5G)",
                        variable=self.wifi_split).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        r = ttk.Frame(P["net"])
        r.pack(fill="x", pady=(0, 10))
        ttk.Button(r, text="Применить интернет и Wi-Fi", style="Accent.TButton", command=self.start_net).pack(
            side="left")
        self.show_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="Показать пароли", variable=self.show_pw, command=self.toggle_passwords).pack(
            side="left", padx=12)
        c = card(P["net"], "DNS — как роутер узнаёт адреса сайтов")
        self.dns_mode = tk.StringVar(value="auto")
        for key in ("auto", "doh", "vpn", "isp"):
            ttk.Radiobutton(c, text=DNS_MODES[key], variable=self.dns_mode, value=key).pack(anchor="w", pady=1)
        r = ttk.Frame(c)
        r.pack(anchor="w", pady=(8, 0))
        ttk.Label(r, text="Сервер зашифрованного DNS").pack(side="left")
        self.doh = ttk.Combobox(r, width=24, state="readonly", values=list(DOH_SERVERS))
        self.doh.current(0)
        self.doh.pack(side="left", padx=6)
        r = ttk.Frame(c)
        r.pack(anchor="w", pady=(8, 0))
        ttk.Button(r, text="Применить DNS", style="Accent.TButton", command=self.start_dns).pack(side="left")
        ttk.Button(r, text="Проверить подмену DNS", command=self.start_dns_check).pack(side="left", padx=8)
        muted(c, "Если провайдер подменяет адреса, zapret не поможет — включите зашифрованный DNS: запросы идут по "
                 "HTTPS, провайдер их не видит. Устройства с «зашитым» DNS тоже перенаправляются на роутер.",
              pady=(8, 0))

        # ===== Роутер и компоненты
        c = card(P["router"], "Компоненты на роутере")
        top = ttk.Frame(c)
        top.pack(fill="x", pady=(0, 6))
        ttk.Button(top, text="Проверить, что установлено", style="Accent.TButton",
                   command=self.start_comp_status).pack(side="left")
        self.comp_free = ttk.Label(top, style="Muted.TLabel", text="")
        self.comp_free.pack(side="left", padx=12)
        g = ttk.Frame(c)
        g.pack(fill="x")
        self.comp_lbl = {}
        for i, (key, title) in enumerate(COMPONENTS):
            ttk.Label(g, text=title, style="Bold.TLabel").grid(row=i, column=0, sticky="w", pady=2)
            lbl = tk.Label(g, text="—", bg=C_CARD, fg=C_MUTED, font=FONT, width=24, anchor="w")
            lbl.grid(row=i, column=1, sticky="w", padx=8)
            self.comp_lbl[key] = lbl
            ttk.Button(g, text="Установить", style="Small.TButton",
                       command=lambda k=key: self.start_comp(k, "install")).grid(row=i, column=2, padx=2)
            if key != "luci_ru":
                ttk.Button(g, text="Удалить", style="Small.TButton",
                           command=lambda k=key: self.start_comp(k, "remove")).grid(row=i, column=3, padx=2)
        c = card(P["router"], "Пароль root (вход по SSH и в веб-панель)")
        r = ttk.Frame(c)
        r.pack(anchor="w")
        ttk.Label(r, text="Новый пароль").pack(side="left")
        self.new_pw = ttk.Entry(r, width=22, show="•")
        self.new_pw.pack(side="left", padx=6)
        ttk.Button(r, text="Задать пароль", command=self.start_root_pw).pack(side="left")
        c = card(P["router"], "Светодиоды и настройки программы")
        r = ttk.Frame(c)
        r.pack(anchor="w")
        ttk.Button(r, text="Выключить светодиоды", command=lambda: self.start_leds("off")).pack(side="left")
        ttk.Button(r, text="Включить", command=lambda: self.start_leds("on")).pack(side="left", padx=(6, 24))
        ttk.Button(r, text="Экспорт настроек", command=self.export_settings).pack(side="left")
        ttk.Button(r, text="Импорт", command=self.import_settings).pack(side="left", padx=6)
        ttk.Button(r, text="Папка данных", command=lambda: open_folder(DATA_DIR)).pack(side="left")
        muted(c, "Поля программы запоминаются сами (пароли и ключи — в зашифрованном виде). Файл экспорта — для "
                 "второго роутера, пароли в нём открытым текстом. «Папка данных» — где лежат настройки, журналы "
                 "и бэкапы роутера.", pady=(8, 0))
        c = card(P["router"], "Опасная зона")
        r = ttk.Frame(c)
        r.pack(anchor="w")
        ttk.Button(r, text="Сбросить OpenWrt к чистым настройкам", style="Danger.TButton",
                   command=self.start_reset).pack(side="left")
        muted(r, "  стираются все настройки и пакеты, прошивка остаётся").pack(side="left")

        # ===== Прошивка и бэкап
        c = card(P["flash"], "Сетевой адаптер (кабель от компьютера к роутеру)")
        r = ttk.Frame(c)
        r.pack(anchor="w")
        self.adapter = ttk.Combobox(r, width=34, state="readonly")
        self.adapter.pack(side="left")
        ttk.Button(r, text="Обновить", style="Small.TButton", command=self.refresh).pack(side="left", padx=6)
        self.btn_dhcp = ttk.Button(r, text="Вернуть DHCP", style="Small.TButton", command=self.restore_dhcp)
        self.btn_dhcp.pack(side="left")
        muted(c, f"На время прошивки IP компьютера станет {PC_IP}. «Вернуть DHCP» — вернуть обычные настройки сети.",
              pady=(6, 0))
        c = card(P["flash"], "Установка OpenWrt")
        muted(c, "Логин сверху — заводской (Admin / Admin). Прошивка OpenWrt 25.12 уже встроена в программу. "
                 "После прошивки логин сам сменится на root.")
        r = ttk.Frame(c)
        r.pack(anchor="w", pady=(8, 0))
        self.btn = ttk.Button(r, text="Установить OpenWrt", style="Accent.TButton", command=self.start)
        self.btn.pack(side="left")
        self.btn_finish = ttk.Button(r, text="Дошить (recovery уже загружен)", command=self.start_finish)
        self.btn_finish.pack(side="left", padx=8)
        c = card(P["flash"], "Резервная копия заводской прошивки")
        r = ttk.Frame(c)
        r.pack(anchor="w")
        self.btn_backup = ttk.Button(r, text="Сделать бэкап", command=self.start_backup)
        self.btn_backup.pack(side="left")
        self.quick = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="Быстрый (без ubi и spi0.0)", variable=self.quick).pack(side="left", padx=8)
        self.btn_restore = ttk.Button(r, text="Восстановить из бэкапа", command=self.start_restore)
        self.btn_restore.pack(side="left", padx=8)

        # ---------- журнал
        lg = ttk.LabelFrame(root, text="Журнал", style="Outer.TLabelframe", padding=(0, 2))
        lg.pack(side="bottom", fill="x", padx=12, pady=(8, 10), before=main)
        self.text = scrolledtext.ScrolledText(lg, state="disabled", height=6, bg="#0b1220", fg="#dbe7ff",
                                              insertbackground="white", font=("Consolas", 9),
                                              relief="flat", bd=0, padx=8, pady=6)
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("err", foreground="#ff8a8a")
        self.text.tag_configure("ok", foreground="#86efac")
        self.text.tag_configure("warn", foreground="#fcd34d")
        self.show_page("home")

        self.refresh()
        self.load_settings()
        self.update_login_hint()
        if not self.wizard_done:
            root.after(700, self.open_wizard)
        self.user.bind("<KeyRelease>", lambda e: self.update_login_hint())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log(f"{APP_NAME} v{APP_VERSION}. Настройки и журналы: {DATA_DIR}")
        self.poll()

    # ---------- настройки
    def _settings_fields(self):
        txt = lambda w: (lambda: w.get("1.0", "end").strip(), lambda v: (w.delete("1.0", "end"), w.insert("1.0", v)))
        ent = lambda w: (lambda: w.get(), lambda v: (w.delete(0, "end"), w.insert(0, v)))
        var = lambda v_: (v_.get, v_.set)

        def combo(w):
            def setter(v):
                if v in w["values"]:
                    w.set(v)
            return (w.get, setter)
        return {
            "router": ent(self.router), "user": ent(self.user), "password": ent(self.pw),
            "adapter": combo(self.adapter), "wan_type": combo(self.wan_type),
            "ppp_user": ent(self.ppp_user), "ppp_pass": ent(self.ppp_pass),
            "ssid": ent(self.ssid), "wifi_key": ent(self.wifi_key), "country": ent(self.country),
            "wifi_split": var(self.wifi_split), "mode": var(self.mode_var), "autotune": var(self.autotune),
            "quick": var(self.quick), "vless": ent(self.vless), "sub": ent(self.sub),
            "engine": var(self.engine), "fk_ext": var(self.fk_ext), "fk_purge": var(self.fk_purge),
            "strategy": txt(self.strategy), "strat_wrap": var(self.strat_wrap),
            "keep_all": var(self.keep_all), "auto_sub": var(self.auto_sub), "lat_switch": var(self.lat_switch),
            "excluded": (lambda: list(self.excluded), lambda v: setattr(self, "excluded", [x for x in (v or [])
                                                                                          if isinstance(x, str)])), "dns_mode": var(self.dns_mode), "doh": combo(self.doh), "user_direct": txt(self.user_direct), "user_proxy": txt(self.user_proxy),
            "sub_servers": (lambda: list(self.sub_servers), self._set_sub_servers),
            "wizard_done": (lambda: self.wizard_done, lambda v: setattr(self, "wizard_done", bool(v))),
        }

    def _set_sub_servers(self, v):
        self.sub_servers = [s for s in (v or []) if isinstance(s, str) and s.startswith("vless://")]
        self.sub_info.configure(text=f"Загружено серверов из подписки: {len(self.sub_servers)}"
                                if self.sub_servers else "")

    def collect_settings(self, plain=False):
        self.derive_mode()
        data = {}
        for k, (get, _) in self._settings_fields().items():
            v = get()
            data[k] = v if plain or k not in SECRET_KEYS else secret_encode(v)
        data["_format"] = "plain" if plain else "local"
        return data

    def apply_settings(self, data):
        plain = data.get("_format") == "plain"
        fields = self._settings_fields()
        for k, v in data.items():
            if k not in fields:
                continue
            if k in SECRET_KEYS and not plain:
                v = secret_decode(v) if isinstance(v, str) else None
                if v is None:
                    continue
            try:
                fields[k][1](v)
            except (tk.TclError, TypeError, ValueError):
                pass
        self.sync_from_mode()
        self.update_login_hint()

    def load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.apply_settings(json.load(f))
        except (OSError, ValueError):
            pass

    def save_settings(self):
        try:
            data = self.collect_settings()
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            if not IS_WIN:
                os.chmod(tmp, 0o600)          # пароли внутри — читать может только владелец
            os.replace(tmp, SETTINGS_FILE)
        except Exception:
            pass

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def export_settings(self):
        path = filedialog.asksaveasfilename(title="Экспорт настроек", defaultextension=".json",
                                            initialfile="openwrt_installer_settings.json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.collect_settings(plain=True), f, ensure_ascii=False, indent=1)
            self.log(f"Настройки сохранены в {path} (пароли внутри открытым текстом — храните файл бережно)")
        except OSError as e:
            messagebox.showerror("Экспорт", str(e))

    def import_settings(self):
        path = filedialog.askopenfilename(title="Импорт настроек", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                self.apply_settings(json.load(f))
            self.save_settings()
            self.log(f"Настройки загружены из {path}")
        except (OSError, ValueError) as e:
            messagebox.showerror("Импорт", f"Не удалось прочитать файл: {e}")

    def open_wizard(self):
        if self.wizard is not None and self.wizard.win.winfo_exists():
            self.wizard.win.lift()
            return
        self.wizard = Wizard(self)

    # ---------- логин
    def set_login(self, user, pw):
        self.user.delete(0, "end")
        self.user.insert(0, user)
        self.pw.delete(0, "end")
        self.pw.insert(0, pw)
        self.update_login_hint()
        self.save_settings()

    def set_openwrt_login(self, *_):
        if self.user.get().strip() != "root":
            self.set_login("root", "")
            self.log("Логин переключён на root (пароль пустой) — так входят в установленный OpenWrt")

    def update_login_hint(self):
        u = self.user.get().strip()
        if PANEL:
            self.login_hint.configure(text="root и ваш пароль роутера (не задавали — пусто)")
            return
        self.login_hint.configure(text=("логин OpenWrt" if u == "root" else
                                        "заводской; после прошивки → root" if u == "Admin" else
                                        "свой логин"))

    def toggle_passwords(self):
        s = "" if self.show_pw.get() else "•"
        for w in (self.pw, self.ppp_pass, self.wifi_key, self.new_pw):
            w.configure(show=s)

    # ---------- состояние
    def start_status(self):
        if self.busy:
            return
        self.run_bg(router_status, self._router_cfg(), None, on_done=self.show_status)

    def show_status(self, rows):
        for w in self.st_grid.winfo_children():
            w.destroy()
        bad = 0
        for i, (name, text, ok) in enumerate(rows):
            color = C_TEXT if ok is None else (C_OK if ok else C_BAD)
            mark = "●" if ok is not None else "○"
            tk.Label(self.st_grid, text=mark, fg=color if ok is not None else "#94a3b8", bg=C_CARD,
                     font=FONT_B).grid(row=i, column=0, sticky="w", padx=(0, 6))
            ttk.Label(self.st_grid, text=name, style="Bold.TLabel").grid(row=i, column=1, sticky="w", padx=(0, 16))
            tk.Label(self.st_grid, text=text, fg=color, bg=C_CARD, font=FONT, anchor="w",
                     justify="left").grid(row=i, column=2, sticky="w")
            bad += ok is False
        self.head_state.configure(text="● всё в порядке" if not bad else f"● проблем: {bad}",
                                  fg="#4ade80" if not bad else "#f87171")

    # ---------- новые действия
    def forget_keys(self):
        if self.busy:
            return
        ans = messagebox.askyesnocancel(
            "Удалить ключ и подписку",
            "Из программы будут удалены vless-ссылка, ссылка на подписку и список её серверов.\n\n"
            "Удалить их ещё и с роутера (VPN выключится)?\n\n"
            "Да — из программы и с роутера\nНет — только из программы\nОтмена — ничего не делать",
            icon="warning", default="no")
        if ans is None:
            return
        cfg = self._router_cfg()
        for w in (self.vless, self.sub):
            w.delete(0, "end")
        self._set_sub_servers([])
        try:
            os.remove(SUB_FILE)
        except OSError:
            pass
        self.save_settings()
        self.log("Ключ, подписка и список серверов удалены из программы")
        if ans:
            self.run_bg(vpn_forget, cfg, "Ключи удалены с роутера, VPN выключен.")

    def start_dns(self):
        if self.busy:
            return
        self.run_bg(dns_apply, self._router_cfg(), "DNS настроен. Выключите и включите Wi-Fi на устройствах, "
                    "чтобы они забыли старые адреса.")

    def start_dns_check(self):
        if self.busy:
            return
        self.run_bg(dns_check, self._router_cfg(), "Проверка завершена — результат в журнале.")

    def start_sub_update(self):
        if self.busy:
            return
        self.run_bg(sub_update_now, self._router_cfg(), "Подписка на роутере обновлена (подробности в журнале).")

    def start_comp_status(self):
        if self.busy:
            return
        self.run_bg(components_status, self._router_cfg(), None, on_done=self.show_components)

    def show_components(self, res):
        if not res:
            return
        for key, (inst, run, extra) in res["items"].items():
            lbl = self.comp_lbl.get(key)
            if not lbl:
                continue
            if not inst:
                lbl.configure(text="не установлен", fg=C_MUTED)
            elif key == "luci_ru":
                lbl.configure(text="установлен", fg=C_OK)
            else:
                lbl.configure(text=("● работает" if run else "○ установлен, выключен") + (f" ({extra})" if extra else ""),
                              fg=C_OK if run else C_WARN)
        self.comp_free.configure(text=res.get("free", ""))
        self.last_components = res

    def start_comp(self, key, action):
        if self.busy:
            return
        title = dict(COMPONENTS)[key]
        if action == "remove":
            if not messagebox.askyesno("Удаление", f"Удалить с роутера: {title}?", icon="warning", default="no"):
                return
        elif key == "forkop":
            if not messagebox.askokcancel(
                    "Forkop", "Программа установит Forkop с GitHub, выключит встроенный VPN"
                    + (" (xray и geo-базы будут удалены)" if self.fk_purge.get() else "") + " и создаст разделы "
                    "по настройкам и ключу из раздела «Обход блокировок».\n"
                    "Нужен интернет на роутере и OpenWrt 24.10+. Займёт 3–10 минут.\n\nПродолжить?"):
                return
            self.engine.set("forkop")
        if key == "forkop" and action == "remove" and self.engine.get() == "forkop":
            self.engine.set("builtin")
            self.log("Движок переключён на встроенный VPN — нажмите «Применить» в «Обходе блокировок», чтобы включить его")
        cfg = self._router_cfg()
        cfg.update(comp=key, action=action)

        def work(c, log):
            component_action(c, log)
            return components_status(c, log)
        self.run_bg(work, cfg, f"Готово: {title} — " + ("установлен." if action == "install" else "удалён."),
                    on_done=self.show_components)

    def open_forkop(self):
        import webbrowser
        webbrowser.open(f"http://{self.router.get().strip() or '192.168.1.1'}{FORKOP_LUCI_PATH}")

    def start_get_strategy(self):
        if self.busy:
            return

        def done(cur):
            self.strategy.delete("1.0", "end")
            self.strategy.insert("1.0", cur or "")
        self.run_bg(zapret_get_strategy, self._router_cfg(), None, on_done=done)

    def start_set_strategy(self):
        if self.busy:
            return
        cfg = self._router_cfg()
        cfg["strategy"] = self.strategy.get("1.0", "end")
        cfg["wrap"] = self.strat_wrap.get()
        try:
            normalize_strategy(cfg["strategy"])
        except StepError as e:
            messagebox.showerror("Стратегия", str(e))
            return
        self.run_bg(zapret_set_strategy, cfg, "Стратегия применена. Проверьте YouTube и Discord на устройствах.")

    def start_geo(self):
        if self.busy:
            return
        self.run_bg(geo_update, self._router_cfg(), "Geo-базы обновлены.")

    def start_root_pw(self):
        if self.busy:
            return
        pw = self.new_pw.get()
        if len(pw) < 6:
            messagebox.showerror("Пароль", "Введите новый пароль (минимум 6 символов).")
            return
        cfg = self._router_cfg()
        cfg["new_pw"] = pw

        def done(_):
            self.set_login("root", pw)
            self.new_pw.delete(0, "end")
        self.run_bg(set_root_password, cfg, "Пароль root изменён и подставлен в поле «Пароль».", on_done=done)

    def start_russian(self):
        if self.busy:
            return
        self.run_bg(luci_russian, self._router_cfg(), "Панель переведена на русский. Обновите страницу (Ctrl+F5).")

    def start_reset(self):
        if self.busy:
            return
        if not messagebox.askyesno(
                "Сброс OpenWrt", "Будут стёрты ВСЕ настройки роутера: Wi-Fi, интернет (PPPoE), VPN, zapret, "
                "пароль.\nПрошивка OpenWrt останется.\n\nПосле сброса: 192.168.1.1, логин root без пароля, "
                "Wi-Fi выключен — настраивайте заново через эту программу.\n\nСбросить?",
                icon="warning", default="no"):
            return
        self.run_bg(factory_reset, self._router_cfg(), "Роутер сбрасывается и перезагружается (2–3 минуты).",
                    on_done=self.set_openwrt_login)

    def refresh(self):
        names = list_adapters()
        self.adapter["values"] = names
        if names and not self.adapter.get():
            self.adapter.current(0)

    def log(self, msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
        with self.lock:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        self.q.put(line)

    def poll(self):
        try:
            while True:
                line = self.q.get_nowait()
                low = line.lower()
                tag = ("err" if ("ошибка" in low or "не удалось" in low) else
                       "warn" if "внимание" in low else
                       "ok" if ("готово" in low or "✔" in line or "применён" in low) else None)
                self.text.configure(state="normal")
                self.text.insert("end", line + "\n", tag)
                self.text.see("end")
                self.text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def start(self):
        if self.busy:
            return
        if not any(os.listdir(d) for d in find_backups()):
            if not messagebox.askyesno(
                    "Нет бэкапа",
                    "Бэкап роутера не найден.\n"
                    "Без бэкапа (особенно Factory) роутер нельзя вернуть в исходное состояние.\n\n"
                    "Продолжить БЕЗ бэкапа?", icon="warning", default="no"):
                return
        if not messagebox.askokcancel("Предупреждение", WARNING_TEXT, icon="warning"):
            return
        cfg = dict(adapter=self.adapter.get(), router=self.router.get().strip(),
                   user=self.user.get().strip(), password=self.pw.get())
        self.run_bg(install, cfg, "Прошивка завершена. Логин в программе сменён на root.\n"
                    "Подождите 3–5 минут, пока роутер загрузится.", on_done=self.set_openwrt_login)

    def set_busy(self, on):
        self.busy = on
        if on:
            self.progress.pack(side="right", padx=(0, 6), before=self.head_state)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self.btn.configure(state="disabled" if on else "normal")

    def run_bg(self, fn, cfg, ok_msg, on_done=None):
        """Запускает fn(cfg, log) в фоне. ok_msg — окно «Готово» (None — без окна); on_done(результат)."""
        self.save_settings()
        self.set_busy(True)
        self.last_ok = None

        def work():
            try:
                res = fn(cfg, self.log)
                self.last_ok = True
                if on_done:
                    self.root.after(0, lambda: on_done(res))
                if ok_msg:
                    self.root.after(0, lambda: messagebox.showinfo("Готово", ok_msg))
            except StepError as e:
                self.last_ok = False
                self.log(f"ОШИБКА: {e}")
                self.root.after(0, lambda m=str(e): messagebox.showerror("Остановлено", m))
            except Exception as e:
                self.last_ok = False
                self.log(f"НЕОЖИДАННАЯ ОШИБКА: {type(e).__name__}: {e}")
                self.root.after(0, lambda m=f"{type(e).__name__}: {e}": messagebox.showerror("Ошибка", m))
            finally:
                self.root.after(0, lambda: (self.set_busy(False), self.save_settings()))
        threading.Thread(target=work, daemon=True).start()

    def _router_cfg(self):
        link = self.vless.get().strip()
        from_sub = link in self.sub_servers
        servers = list(self.sub_servers) if (self.keep_all.get() and from_sub) else []
        sub_url = self.sub.get().strip() if (self.auto_sub.get() and from_sub) else ""
        return dict(router=self.router.get().strip(), user=self.user.get().strip(),
                    password=self.pw.get(), vless=link, servers=servers, sub_url=sub_url,
                    excluded=list(self.excluded), lat_switch=self.lat_switch.get(),
                    dns_mode=self.dns_mode.get(), doh=self.doh.get(), engine=self.engine.get(),
                    sub=self.sub.get().strip(), singbox_extended=self.fk_ext.get(), purge_xray=self.fk_purge.get(),
                    mode=self.mode_var.get(),
                    user_direct=self.user_direct.get("1.0", "end"), user_proxy=self.user_proxy.get("1.0", "end"))

    def receive_from_phone(self):
        if getattr(self, "recv_win", None) is not None:
            return
        rcv = LinkReceiver()
        try:
            rcv.start()
        except OSError as e:
            messagebox.showerror("Приём ссылки", f"Не удалось открыть порт {RECV_PORT}: {e}")
            return
        router_ip = self.router.get().strip() or "192.168.1.1"
        fw_allow(RECV_RULE, "TCP", RECV_PORT)
        fw_allow(RECV_RULE, program=sys.executable)
        # Если ноутбук одновременно в другой сети (домашний Wi-Fi) с такими же адресами 192.168.1.x,
        # ответы телефону уходят не в тот адаптер — страница не грузится. Даём адаптеру роутера приоритет.
        adapter = self.adapter.get()
        metric_set = False
        if adapter and IS_WIN:
            c, _ = run_cmd(['powershell', '-NoProfile', '-Command',
                            f"Set-NetIPInterface -InterfaceAlias '{adapter}' -AddressFamily IPv4 -InterfaceMetric 1"])
            metric_set = c == 0
        ip = local_ip_towards(router_ip)
        ips = [ip] + [i for i in local_ips(router_ip) if i != ip]
        url = f"http://{ip}:{RECV_PORT}"
        prefix = ".".join(router_ip.split(".")[:3]) + "."
        same_net = [i for i in ips if i.startswith(prefix)]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{RECV_PORT}/", timeout=3).read()
            self_test = "сервер на ноутбуке работает"
        except Exception as e:
            self_test = f"ОШИБКА самопроверки: {e}"
        win = tk.Toplevel(self.root)
        self.recv_win = win
        win.title("Приём ссылки с телефона")
        win.configure(bg="white")
        F = ("Segoe UI", 11)
        FB = ("Segoe UI", 12, "bold")

        def L(text, font=F, **kw):
            w = tk.Label(win, text=text, font=font, fg="black", bg="white", justify="left", **kw)
            w.pack(padx=14, pady=3, anchor="w")
            return w
        L("1) Подключите телефон к Wi-Fi этого роутера\n"
          "2) Наведите камеру телефона на QR-код или откройте адрес ниже\n"
          "3) Вставьте vless-ссылку и нажмите «Отправить»", FB)
        if segno is not None:
            try:
                buf = io.BytesIO()
                segno.make(url, error="m").save(buf, kind="png", scale=6, border=3)
                img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))
                lbl = tk.Label(win, image=img, bg="white")
                lbl.image = img
                lbl.pack(pady=6)
            except Exception as e:
                L(f"(QR не построился: {e})")
        else:
            L("(QR недоступен: установите segno — pip install segno)")
        L("Адрес для браузера телефона:")
        ent = tk.Entry(win, width=30, font=("Consolas", 16, "bold"), fg="black", bg="white",
                       readonlybackground="white", justify="center", relief="solid", bd=1)
        ent.insert(0, url)
        ent.configure(state="readonly")
        ent.pack(padx=14, pady=6)
        others = [f"http://{i}:{RECV_PORT}" for i in ips[1:]]
        if others:
            L("Если не открывается, попробуйте другой адрес:\n" + "\n".join(others))
        if len(same_net) > 1:
            L("Внимание: ноутбук подключён к двум сетям с адресами " + prefix + "x.\n"
              + ("Адаптеру роутера дан приоритет. " if metric_set else "")
              + "Если страница всё равно не грузится — отключите на ноутбуке\nдругой Wi-Fi/интернет "
              "на время приёма ссылки.", FB)
        status = L(f"Проверка: {self_test}\nЗапросов с телефона: 0", FB)
        L("Не открывается / белая страница?\n"
          "• телефон должен быть в Wi-Fi этого роутера; выключите на нём мобильный интернет\n"
          "• если на ноутбуке есть другой интернет (Wi-Fi), отключите его на время приёма\n"
          "• если «Запросов» остаётся 0 — запрос не доходит до ноутбука (брандмауэр Windows)\n"
          "• либо отсканируйте QR камерой ноутбука / вставьте ссылку вручную в разделе «Обход блокировок»")

        def close():
            rcv.stop()
            fw_remove(RECV_RULE, "TCP", RECV_PORT)
            if metric_set:
                run_cmd(['powershell', '-NoProfile', '-Command',
                         f"Set-NetIPInterface -InterfaceAlias '{adapter}' -AddressFamily IPv4 "
                         "-AutomaticMetric Enabled"])
            self.recv_win = None
            win.destroy()

        tk.Button(win, text="Закрыть", font=FB, command=close).pack(pady=(6, 12))
        win.protocol("WM_DELETE_WINDOW", close)

        def poll():
            if self.recv_win is None:
                return
            status.configure(text=f"Проверка: {self_test}\nЗапросов с телефона: {rcv.hits}"
                                  + (f" (последний с {rcv.last_ip})" if rcv.hits else ""))
            if rcv.event.is_set():
                link = rcv.result
                close()
                self.put_link(link, "Телефон")
                return
            win.after(500, poll)
        poll()

    def put_link(self, link, source):
        """Вставляет полученную ссылку: vless — в поле ключа, https — в подписку и сразу грузит серверы."""
        if is_sub_url(link):
            self.sub.delete(0, "end")
            self.sub.insert(0, link)
            self.log(f"{source}: получена ссылка на подписку, загружаю серверы")
            self.load_subscription()
        else:
            self.vless.delete(0, "end")
            self.vless.insert(0, link)
            self.log(f"{source}: vless-ссылка вставлена (в лог не записывается)")
            messagebox.showinfo("Готово", "Ссылка вставлена в поле.\n"
                                "Теперь нажмите «Применить» в разделе «Обход блокировок».")

    def load_subscription(self):
        if self.busy:
            messagebox.showinfo("Подписка", "Дождитесь окончания текущей операции и нажмите «Загрузить серверы».")
            return
        cfg = self._router_cfg()
        cfg["sub_url"] = self.sub.get().strip()
        if not is_sub_url(cfg["sub_url"]):
            messagebox.showerror("Подписка", "Вставьте ссылку на подписку вида https://...")
            return
        self.set_busy(True)

        def work():
            try:
                servers = fetch_subscription(cfg, self.log)
                self.root.after(0, lambda: (self._set_sub_servers([x[3] for x in servers]),
                                            self.save_settings(), self.pick_server(servers)))
            except StepError as e:
                self.log(f"ОШИБКА: {e}")
                self.root.after(0, lambda m=str(e): messagebox.showerror("Подписка", m))
            except Exception as e:
                self.log(f"НЕОЖИДАННАЯ ОШИБКА: {type(e).__name__}: {e}")
                self.root.after(0, lambda m=f"{type(e).__name__}: {e}": messagebox.showerror("Подписка", m))
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def pick_server(self, servers):
        win = tk.Toplevel(self.root)
        win.title("Выбор сервера из подписки")
        win.configure(bg="white")
        lat = {}
        tk.Label(win, text=f"Серверов в подписке: {len(servers)}. Выберите один (двойной щелчок или кнопка):",
                 font=("Segoe UI", 11, "bold"), fg="black", bg="white").pack(padx=12, pady=(12, 6), anchor="w")
        fr = tk.Frame(win, bg="white")
        fr.pack(fill="both", expand=True, padx=12)
        lb = tk.Listbox(fr, width=96, height=min(18, max(6, len(servers))), font=("Segoe UI", 11),
                        fg="black", bg="white", selectbackground=C_ACCENT, activestyle="none")
        sb = ttk.Scrollbar(fr, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cur = self.vless.get().strip()

        def fill():
            sel = lb.curselection()
            lb.delete(0, "end")
            for i, (name, addr, kind, link) in enumerate(servers):
                ms = ("" if i not in lat else "   нет ответа" if lat[i] is None else f"   {lat[i]} мс")
                ex = name in self.excluded
                lb.insert("end", ("✓ " if link == cur else "⛔ " if ex else "   ") + f"{name}    —    {addr}    ({kind}){ms}"
                          + ("    [исключён]" if ex else ""))
                if ex:
                    lb.itemconfigure(i, fg="#b91c1c")
                elif lat.get(i) is None and i in lat:
                    lb.itemconfigure(i, fg="#9ca3af")
            lb.selection_set(sel[0] if sel else 0)
        fill()
        status = tk.Label(win, text="«Проверить задержку» замеряет отклик серверов с роутера.",
                          font=("Segoe UI", 10), fg="#334155", bg="white")
        status.pack(padx=12, pady=(6, 0), anchor="w")

        def choose(_e=None, idx=None):
            sel = [idx] if idx is not None else lb.curselection()
            if not sel:
                return
            name, addr, kind, link = servers[sel[0]]
            if name in self.excluded:
                messagebox.showwarning("Сервер исключён", f"«{name}» исключён. Сначала нажмите «Исключить / вернуть», "
                                       "чтобы вернуть его.", parent=win)
                return
            self.vless.delete(0, "end")
            self.vless.insert(0, link)
            self.save_settings()
            self.log(f"Выбран сервер из подписки: {name} ({addr})")
            win.destroy()
            messagebox.showinfo("Сервер выбран", f"«{name}» вставлен в поле ключа.\n"
                                "Теперь нажмите «Применить» в разделе «Обход блокировок».\n"
                                "Старый ключ на роутере будет заменён, маршрутизация останется прежней.")

        def ping():
            if self.busy:
                return
            status.configure(text="Замер задержки с роутера… (до 10 секунд)")
            cfg = self._router_cfg()
            cfg["servers_list"] = servers

            def done(res):
                lat.clear()
                lat.update({i: res.get(i) for i in range(len(servers))})
                if win.winfo_exists():
                    fill()
                    ok = [i for i in lat if lat[i]]
                    status.configure(text=f"Отвечают {len(ok)} из {len(servers)}."
                                     + (f" Быстрее всех: {servers[min(ok, key=lat.get)][0]}" if ok else ""))
            self.run_bg(server_latency, cfg, None, on_done=done)

        def toggle_excl():
            sel = lb.curselection()
            if not sel:
                return
            name = servers[sel[0]][0]
            if name in self.excluded:
                self.excluded.remove(name)
                self.log(f"Сервер «{name}» возвращён в список")
            else:
                if servers[sel[0]][3] == cur:
                    messagebox.showwarning("Исключение", "Это текущий сервер. Сначала выберите другой.", parent=win)
                    return
                self.excluded.append(name)
                self.log(f"Сервер «{name}» исключён — роутер не будет его использовать")
            self.save_settings()
            fill()
            status.configure(text=f"Исключено серверов: {len(self.excluded)}. Чтобы роутер узнал об этом, "
                                  "нажмите «Применить» в разделе «Обход блокировок».")

        def fastest():
            ok = [i for i in lat if lat[i] and servers[i][0] not in self.excluded]
            if not ok:
                messagebox.showinfo("Сервер", "Сначала нажмите «Проверить задержку».", parent=win)
                return
            choose(idx=min(ok, key=lat.get))
        lb.bind("<Double-Button-1>", choose)
        bf = tk.Frame(win, bg="white")
        bf.pack(pady=10)
        ttk.Button(bf, text="Выбрать", style="Accent.TButton", command=choose).pack(side="left", padx=4)
        ttk.Button(bf, text="Проверить задержку", command=ping).pack(side="left", padx=4)
        ttk.Button(bf, text="Выбрать самый быстрый", command=fastest).pack(side="left", padx=4)
        ttk.Button(bf, text="Исключить / вернуть", style="Danger.TButton", command=toggle_excl).pack(side="left", padx=4)
        ttk.Button(bf, text="Отмена", command=win.destroy).pack(side="left", padx=4)

    def scan_qr_camera(self):
        if cv2 is None:
            messagebox.showerror("Камера", "Не установлена библиотека OpenCV.\n"
                                 "Выполните: pip install opencv-python-headless\n"
                                 "Или вставьте ссылку вручную / примите её с телефона.")
            return
        if getattr(self, "cam_win", None) is not None:
            return
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(0)
        if not cap.isOpened():
            messagebox.showerror("Камера", "Не удалось открыть камеру. Проверьте, что она подключена, "
                                 "и разрешите доступ в Параметры Windows → Конфиденциальность → Камера.")
            return
        win = tk.Toplevel(self.root)
        self.cam_win = win
        win.title("Сканирование QR-кода")
        win.configure(bg="white")
        status = tk.Label(win, text="Наведите камеру на QR-код с vless-ссылкой или подпиской",
                          font=("Segoe UI", 11, "bold"), fg="black", bg="white")
        status.pack(padx=10, pady=(10, 4))
        view = tk.Label(win, bg="white")
        view.pack(padx=10)
        det = cv2.QRCodeDetector()

        def close():
            self.cam_win = None
            try:
                cap.release()
            except Exception:
                pass
            win.destroy()

        ttk.Button(win, text="Закрыть", command=close).pack(pady=10)
        win.protocol("WM_DELETE_WINDOW", close)

        def tick():
            if self.cam_win is None:
                return
            ok, frame = cap.read()
            if ok:
                try:
                    data, _, _ = det.detectAndDecode(frame)
                except Exception:
                    data = ""
                if data:
                    link = data.strip()
                    try:
                        link_kind(link)
                    except StepError:
                        status.configure(text="QR найден, но это не vless-ссылка и не подписка. Пробуйте другой.")
                    else:
                        close()
                        self.put_link(link, "Камера")
                        return
                small = cv2.resize(frame, (480, 360))
                good, png = cv2.imencode(".png", small)
                if good:
                    img = tk.PhotoImage(data=base64.b64encode(png.tobytes()).decode("ascii"))
                    view.configure(image=img)
                    view.image = img
            win.after(60, tick)
        tick()

    def start_finish(self):
        if self.busy:
            return
        if not messagebox.askokcancel("Дошить", "Используйте, если recovery уже загружен (роутер отвечает на "
                                      "192.168.1.1, а sysupgrade не прошёл).\nНе выключайте питание!\n\nПродолжить?"):
            return
        self.run_bg(finish_sysupgrade, dict(router=self.router.get().strip(), password=""),
                    "Прошивка запущена. Подождите 3-5 минут. Логин в программе сменён на root.",
                    on_done=self.set_openwrt_login)

    def start_restore(self):
        if self.busy:
            return
        if not self.adapter.get():
            messagebox.showerror("Восстановление", "Выберите сетевой адаптер.")
            return
        backups = find_backups()
        folder = filedialog.askdirectory(
            title="Выберите папку backup_* для восстановления",
            initialdir=backups[-1] if backups else BACKUP_DIR)
        if not folder:
            return
        if not messagebox.askokcancel(
                "Восстановление родной прошивки",
                "Будут записаны обратно разделы ubi, u-boot-env и FIP (загрузчик) из выбранного бэкапа.\n"
                "OpenWrt будет удалён.\n\n"
                "ВАЖНО: НЕЛЬЗЯ отключать питание во время записи. Если процесс прервётся на последнем шаге, "
                "понадобится UART/программатор.\n\nПродолжить?", icon="warning", default="cancel"):
            return
        cfg = dict(adapter=self.adapter.get(), router=self.router.get().strip(),
                   user=self.user.get().strip(), password=self.pw.get(), folder=folder)
        self.run_bg(restore_backup, cfg, "Восстановление завершено. Подождите загрузку роутера.")

    def start_backup(self):
        if self.busy:
            return
        cfg = dict(adapter=self.adapter.get(), router=self.router.get().strip(),
                   user=self.user.get().strip(), password=self.pw.get(), quick=self.quick.get())
        self.run_bg(backup_mtd, cfg, "Бэкап готов (папка открыта). Скопируйте её в надёжное место — флешку или облако.")

    def start_net(self):
        if self.busy:
            return
        wan = {"DHCP (автоматически)": "dhcp", "PPPoE (логин/пароль)": "pppoe"}.get(
            self.wan_type.get(), "skip")
        cfg = self._router_cfg()
        cfg.update(wan_type=wan, ppp_user=self.ppp_user.get().strip(), ppp_pass=self.ppp_pass.get(),
                   ssid=self.ssid.get().strip(), wifi_key=self.wifi_key.get(),
                   country=self.country.get().strip(), wifi_split=self.wifi_split.get())
        self.run_bg(net_setup, cfg, "Настройки применены. Подробности — в логе.")

    def start_vpn(self):
        if self.busy:
            return
        if is_sub_url(self.vless.get()):
            self.put_link(self.vless.get().strip(), "Поле ссылки")
            self.vless.delete(0, "end")
            return
        if self.vless.get().strip() and not self.vless.get().strip().startswith("vless://"):
            messagebox.showerror("VPN", "Вставьте ссылку вида vless://... или загрузите серверы из подписки")
            return
        if not messagebox.askokcancel(
                "VPN", "Роутер должен быть уже прошит и подключён к интернету (кабель в WAN).\n"
                       "Если xray ещё не установлен — он установится; если установлен — "
                       "обновятся только ключ и маршрутизация.\n\nПродолжить?"):
            return
        self.engine.set("builtin")
        cfg = self._router_cfg()
        cfg["zapret_mode"] = self.mode_var.get() == "both"
        self.run_bg(vpn_install, cfg, "VPN настроен. Проверьте Telegram, Instagram и т. п.\n"
                    "Состояние можно посмотреть на странице «Главная».")

    def show_page(self, key):
        for k, pg in self.pages.items():
            pg.outer.pack_forget()
            self.nav_btns[k].configure(bg=C_HEAD, fg="#cbd5e1", font=("Segoe UI", 11))
        self.pages[key].outer.pack(fill="both", expand=True)
        self.nav_btns[key].configure(bg=C_ACCENT, fg="white", font=("Segoe UI", 11, "bold"))
        self.cur_page = key

    def derive_mode(self):
        """VPN (встроенный / Forkop / выкл) + галочка zapret -> mode_var и engine (их понимает остальной код)."""
        v, z = self.vpn_choice.get(), self.zapret_on.get()
        self.engine.set("forkop" if v == "forkop" else "builtin")
        self.mode_var.set("both" if (v != "off" and z) else "vpn" if v != "off" else "zapret" if z else "off")
        return self.mode_var.get()

    def sync_from_mode(self):
        m, e = self.mode_var.get(), self.engine.get()
        self.vpn_choice.set("off" if m in ("zapret", "off") else ("forkop" if e == "forkop" else "builtin"))
        self.zapret_on.set(m in ("zapret", "both"))
        self.update_mode_desc(derive=False)

    def update_mode_desc(self, derive=True):
        m = self.derive_mode() if derive else self.mode_var.get()
        txt = MODE_TEXT[m][1]
        if m in ("vpn", "both") and self.engine.get() == "forkop":
            txt += " Работает через Forkop: какие сайты идут через VPN, задаётся его списками и панелью."
        self.mode_desc.configure(text=txt)

    def start_mode(self):
        if self.busy:
            return
        mode = self.derive_mode()
        if is_sub_url(self.vless.get()):
            self.put_link(self.vless.get().strip(), "Поле ссылки")
            self.vless.delete(0, "end")
            return
        if not messagebox.askokcancel(
                "Применить", MODE_TEXT[mode][0] + "\n\nУже установленное не переустанавливается — "
                         "меняется только то, как идёт трафик. Если чего-то ещё нет на роутере, "
                         "оно установится (нужен интернет на роутере, а для zapret и на ноутбуке).\n\n"
                         "Продолжить?"):
            return
        cfg = self._router_cfg()
        cfg["mode"] = mode
        cfg["autotune"] = self.autotune.get()
        self.run_bg(apply_mode, cfg, "Режим применён. Проверьте YouTube/Discord и нужные сайты.")

    def start_leds(self, state):
        if self.busy:
            return
        cfg = self._router_cfg()
        cfg["leds"] = state
        self.run_bg(leds_set, cfg, "Готово.")

    def start_zapret(self):
        if self.busy:
            return
        if not messagebox.askokcancel(
                "Zapret", "Пакеты скачиваются на ноутбук с GitHub и ставятся на роутер.\n"
                          "Нужен интернет и на ноутбуке (нажмите «Вернуть DHCP»), и на роутере (WAN).\n\n"
                          "Продолжить?"):
            return
        self.run_bg(zapret_install, self._router_cfg(), "Zapret установлен. Стратегия: Services → Zapret.")

    def start_tune(self):
        if self.busy:
            return
        if not messagebox.askokcancel(
                "Подбор стратегии", "Zapret будет остановлен, и программа перебёрёт стратегии под вашего "
                "провайдера (5–25 минут). Роутер должен быть подключён к интернету (WAN), а программа и "
                "роутер должны оставаться включёнными.\n\nПродолжить?"):
            return
        self.run_bg(zapret_autotune, self._router_cfg(), "Стратегия подобрана и применена. Проверьте YouTube и Discord.")

    def stop_zapret(self):
        if self.busy:
            return
        self.run_bg(zapret_disable, self._router_cfg(), "Zapret отключён.")

    def diag_vpn(self):
        if self.busy:
            return
        self.run_bg(vpn_diag, self._router_cfg(), "Диагностика завершена. Результат в окне логов.")

    def rollback_vpn(self):
        if self.busy:
            return
        self.run_bg(vpn_rollback, self._router_cfg(), "VPN отключён.")

    def restore_dhcp(self):
        name = self.adapter.get()
        if not name:
            return
        c1, o1 = set_dhcp(name)
        self.log("Адаптер возвращён на DHCP" if c1 == 0 else f"Не удалось вернуть DHCP: {o1}")


def selftest():
    """--selftest [файл]: проверка сборки без окна — версия, встроенные прошивки и их SHA256. Код выхода 0 = всё в порядке."""
    i = sys.argv.index("--selftest")
    out = sys.argv[i + 1] if len(sys.argv) > i + 1 else None
    info = dict(app=APP_NAME, version=APP_VERSION, panel=PANEL, frozen=bool(getattr(sys, "frozen", False)),
                data_dir=DATA_DIR, paramiko=paramiko is not None, firmware={}, errors=[])
    if not PANEL:
        for key, path in find_files().items():
            if not path:
                info["errors"].append("нет файла прошивки: " + key)
                continue
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest, name = h.hexdigest(), os.path.basename(path)
            info["firmware"][key] = dict(file=name, size=os.path.getsize(path), sha256=digest)
            sums = os.path.join(os.path.dirname(path), "SHA256SUMS")
            if os.path.isfile(sums):
                expected = {ln.split()[-1].lstrip("*"): ln.split()[0] for ln in open(sums, encoding="utf-8") if ln.strip()}
                if name in expected and expected[name] != digest:
                    info["errors"].append("SHA256 не совпадает: " + name)
    if paramiko is None:
        info["errors"].append("нет paramiko: " + PARAMIKO_ERROR)
    info["ok"] = not info["errors"]
    text = json.dumps(info, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        try:
            print(text)
        except Exception:
            pass
    return info["ok"]


def main():
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if IS_WIN and not is_admin() and not PANEL:
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, subprocess.list2cmdline(args), None, 1)
        return
    moved = migrate_legacy_files()
    root = tk.Tk()
    app = App(root)
    if moved:
        app.log("Файлы прошлой версии перенесены из папки программы в папку данных: " + ", ".join(moved))
    if not IS_WIN and not is_admin() and not PANEL:
        app.log("ВНИМАНИЕ: программа запущена без root. Настройка VPN, Wi-Fi, DNS и т. п. работает, "
                "а прошивка, бэкап и восстановление — нет. " + ADMIN_HINT)
    root.mainloop()


if __name__ == "__main__":
    main()
