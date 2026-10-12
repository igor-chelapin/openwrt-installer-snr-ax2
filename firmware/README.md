# Прошивка OpenWrt для SNR-CPE-AX2

Официальные образы **OpenWrt 25.12.4**, target `mediatek/filogic`, профиль `snr_snr-cpe-ax2`.
Файлы не изменялись — только скачаны с официального сайта.

| Файл | Что это |
|---|---|
| `openwrt-mediatek-filogic-snr_snr-cpe-ax2-bl31-uboot.fip` | загрузчик (BL31 + U-Boot), пишется в раздел `FIP` |
| `openwrt-mediatek-filogic-snr_snr-cpe-ax2-initramfs-recovery.itb` | recovery-образ, загружается по TFTP |
| `openwrt-mediatek-filogic-snr_snr-cpe-ax2-squashfs-sysupgrade.itb` | основная прошивка (sysupgrade) |

Контрольные суммы — в [SHA256SUMS](SHA256SUMS). Проверка: `sha256sum -c SHA256SUMS` (Linux/macOS) или `certutil -hashfile <файл> SHA256` (Windows).

## Где взять свежую версию

- Подбор прошивки: https://firmware-selector.openwrt.org/?version=25.12.4&target=mediatek%2Ffilogic&id=snr_snr-cpe-ax2
- Каталог файлов: https://downloads.openwrt.org/releases/25.12.4/targets/mediatek/filogic/

Чтобы поставить другую версию, положите три файла с такими же окончаниями (`*bl31-uboot.fip`, `*initramfs-recovery.itb`, `*squashfs-sysupgrade.itb`) в эту папку вместо текущих.

## Лицензия

OpenWrt распространяется под GPL-2.0 и другими свободными лицензиями. Исходный код: https://github.com/openwrt/openwrt (тег `v25.12.4`). Эти файлы не подпадают под GPL-3.0 программы — у них собственные лицензии OpenWrt.
