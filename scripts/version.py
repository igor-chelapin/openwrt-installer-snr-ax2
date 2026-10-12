# Печатает версию программы (APP_VERSION) — для скриптов сборки.
import os
import re
src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "openwrt_installer.py")
print(re.search(r'APP_VERSION = "(.+?)"', open(src, encoding="utf-8").read()).group(1))
