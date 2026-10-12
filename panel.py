#!/usr/bin/env python3
# Панель управления — облегчённая версия OpenWrt Installer только для управления роутером на OpenWrt:
# VPN (xray / Forkop), подписки, zapret, DNS, Wi-Fi, PPPoE, компоненты, состояние.
# Без прошивки, бэкапа и прав администратора.
import openwrt_installer as core

core.set_edition("panel")

if __name__ == "__main__":
    core.main()
