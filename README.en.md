<p align="center"><img src="assets/icon-256.png" width="128" alt="icon"></p>

<h1 align="center">OpenWrt Installer SNR AX2</h1>

<p align="center">
A desktop app that flashes the <b>SNR-CPE-AX2</b> router with OpenWrt and turns it into a home router with VPN (xray / vless), DPI bypass (zapret) and private DNS — no command line needed.
<br><br>
Windows 7 / 10 / 11 · Linux · macOS · <a href="README.md">Русский</a>
</p>

> **Status: beta.** Non-commercial open-source project (GPL-3.0). The UI is in Russian.

## Features

- **Flashing:** fully automatic OpenWrt install from stock firmware (bootloader → recovery via built-in TFTP → sysupgrade), full **backup of stock firmware** and **restore** from it, first-run **setup wizard** (every step can be skipped).
- **Internet & Wi-Fi:** PPPoE / DHCP, Wi-Fi name and password, root password, Russian LuCI.
- **VPN (xray):** vless links and **subscriptions** (Reality, TLS, WS, gRPC, XHTTP, HTTPUpgrade); receive a link from a phone via a local web page/QR or scan a QR code with the laptop camera. Routing: Russian sites/IPs, Google (except YouTube/Gemini), Apple, Microsoft and torrents go direct, everything else via VPN; custom exceptions.
- **Self-healing on the router:** automatic server failover and switching away from slow servers, subscription auto-update every 6 h (ucode), weekly geo database update, server exclusion list.
- **zapret:** install from GitHub and automatic strategy selection; mode “zapret + VPN” sends YouTube/Discord through zapret and falls back to VPN when zapret stops working.
- **DNS:** encrypted DNS (DoH: Cloudflare, Google, Quad9, AdGuard) or DNS through the VPN tunnel; DNS spoofing check.
- Status tab, diagnostics, LEDs on/off, OpenWrt reset, settings export/import.

## OpenWrt Panel — management-only edition

**OpenWrt Panel** is the same app without flashing: VPN, subscriptions, zapret, DNS, Wi-Fi, status and the setup wizard. No admin rights needed; works with **any router already running OpenWrt** with ≥256 MB RAM and ≥128 MB flash. Download the `OpenWrtPanel-…` archives from Releases. See [docs/ROUTERS.md](docs/ROUTERS.md) (in Russian).

## Quick start

1. Download the file for your OS from the **[latest release](../../releases/latest)** (`windows-x64`, `windows7-x64`, `linux-x64`, `macos-arm64`; Intel Macs — run from source). Windows has a `Setup.exe` installer and a single portable `.exe`; `…-all-files.zip` contains the whole project. The OpenWrt firmware is **built into the program** (a single file). Settings, logs and backups are kept in the user data folder (Windows: `%LOCALAPPDATA%\OpenWrtInstaller`, Linux: `~/.local/share/OpenWrtInstaller`, macOS: `~/Library/Application Support/OpenWrtInstaller`); put an empty `portable.txt` next to the program to keep them beside it instead.
2. Connect the laptop to a **LAN** port of the router (ISP cable → **WAN**).
3. Run it: Windows — `OpenWrtInstaller.exe` (asks for admin rights); Linux — `sudo -E ./OpenWrtInstaller`; macOS — right-click `OpenWrtInstaller.app` → Open (root is needed only for flashing: `sudo OpenWrtInstaller.app/Contents/MacOS/OpenWrtInstaller`).
4. Follow the setup wizard.

From source (needs Git and Python 3.8+):

```
git clone https://github.com/igor-chelapin/panel-setup.git
cd panel-setup
run.bat          # Windows
./run.sh         # Linux / macOS (sudo ./run.sh for flashing)
```

The first run creates `.venv` and installs dependencies; `git pull` updates. Build instructions: [docs/BUILD.md](docs/BUILD.md).

## Warning

Flashing can brick the router (especially on power loss while writing the bootloader) and usually voids the warranty. Make a backup first. Provided **without any warranty** (see [LICENSE](LICENSE)). Use it in accordance with the laws of your country. The firmware in `firmware/` is for **SNR-CPE-AX2 only**.

## License

[GNU GPL v3.0](LICENSE). Firmware images in `firmware/` are official OpenWrt builds (GPL-2.0, sources: https://github.com/openwrt/openwrt).
