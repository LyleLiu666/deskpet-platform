# 兼容开发板和 USB 刷机

当前镜像面向 **ESP32-2432S028 双 USB 版**，使用经典 ESP32、4 MiB Flash、ST7789 屏幕和 XPT2046 电阻触摸。购买或刷机前核对屏幕控制器、触摸控制器和接线。同系列存在其他接线和屏幕版本。

| 信号 | GPIO |
| --- | --- |
| 屏幕 SPI SCK / MISO / MOSI / CS | 14 / 12 / 13 / 15 |
| 屏幕 DC / 背光 | 2 / 21 |
| 触摸 IRQ | 36 |

## 选择刷机包

- **首次刷机包**用于空白开发板，包含引导程序、分区表、启动数据和应用镜像。刷写启动数据会重置启动分区选择。已有固件的小板请先备份 Flash 和储存卡。
- **升级包**用于装过兼容镜像的小板。先读回分区表并与包内校验值比对，通过后只更新应用区。Wi-Fi、配对设置和储存卡文件位于其他存储区域。

在 [RC1 下载页](https://github.com/LyleLiu666/deskpet-platform/releases/tag/v0.1.0-rc1) 可取得电脑端安装包与当前固件 `rc.54`。空白板选 `deskpet-esp32-2432s028-0.1.0-rc.54-factory.zip`。运行 `rc.53` 的兼容小板选 `deskpet-esp32-2432s028-0.1.0-rc.54-upgrade.zip`，解压后按包内 README 校验分区表并更新。下载后可用同页的 `SHA256SUMS-firmware-rc.54.txt` 核对 ZIP 的 SHA-256。已安装 `rc.54` 的小板可直接进入[配网与配对](../src/deskpet_platform/manuals/operations.md#手机扫码配网和配对)。

## 首次刷机

1. 对照上面的板型和接线。用支持数据传输的 USB 线连接小板的数据口。Windows 可在设备管理器的「端口」中查新出现的 `COM` 号；macOS 可在插拔前后用 `ls /dev/cu.*` 对照新出现的串口。关闭占用该串口的工具或 Agent。
2. 解压首刷包，进入解压目录。macOS 用 `shasum -a 256 -c SHA256SUMS` 校验镜像；Windows 在 PowerShell 运行 `Get-ChildItem -Filter *.bin | Get-FileHash -Algorithm SHA256`，逐项与包内 `SHA256SUMS` 比对。全部匹配后再继续。
3. 安装 Python 后，在解压目录创建刷机环境。macOS 运行 `python3 -m venv .flash-venv`，再运行 `source .flash-venv/bin/activate`。Windows PowerShell 运行 `py -3 -m venv .flash-venv`，以下所有 `python` 换成 `.\.flash-venv\Scripts\python.exe`。然后用 `python -m pip install esptool==4.5.1` 安装刷机工具。
4. 将下面的 `PORT` 换成实际串口，例如 Windows 的 `COM5` 或 macOS 的 `/dev/cu.usbserial-140`。先运行第一行，确认识别到 ESP32 和 4 MB Flash，再运行第二行写入并校验。已有固件时，按上节选择升级包。

```sh
python -m esptool --chip esp32 --port PORT flash_id
python -m esptool --chip esp32 --port PORT write_flash --verify 0x1000 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 firmware.bin
```

如果找不到串口，先换数据线或数据口。若识别到串口但无法连接芯片，对照板型说明进入下载模式后重试。写入校验成功后，重启小板，打开 Wi-Fi 设置，用手机扫码配网。让电脑与小板处于同一局域网，再按[操作手册](../src/deskpet_platform/manuals/operations.md#手机扫码配网和配对)完成配对。最后让 Agent 查询设备状态并让小板显示一行测试文字。
