# 兼容开发板与 USB 刷机说明

搭搭（DeskPet）当前固件面向 **ESP32-2432S028 双 USB 版** 开发板（经典 ESP32 芯片、4 MiB Flash、ST7789 屏幕控制器、XPT2046 电阻触摸控制器）。同系列开发板存在不同屏幕驱动与引脚版本，购买或刷机前请先核对以下硬件引脚定义：

| 信号 | GPIO 引脚 |
| --- | --- |
| 屏幕 SPI（SCK / MISO / MOSI / CS） | 14 / 12 / 13 / 15 |
| 屏幕 DC / 背光（BL） | 2 / 21 |
| 触摸中断（IRQ） | 36 |

---

## 1. 选择刷机包

请根据手里设备的状态选择对应的刷机包（若开发板中已有重要数据，刷机前请先备份 Flash 与 TF 储存卡）：

- **首次刷机包（空白开发板使用）**：
  文件名形如 `deskpet-esp32-2432s028-<版本>-factory.zip`。包含同一次构建生成的引导程序（`bootloader.bin`）、分区表（`partitions.bin`）、启动数据（`boot_app0.bin`）和应用镜像（`firmware.bin`）。刷写启动数据会重置启动分区选择。
- **固件升级包（已刷过搭搭固件的设备使用）**：
  文件名形如 `deskpet-esp32-2432s028-<版本>-upgrade.zip`（或发布页单独提供的 `firmware-<修订>.bin`）。升级时先读回板端分区表并校验一致后，仅更新 `0x10000` 应用区，**保留**原有的 Wi-Fi 配置、电脑配对密钥和 TF 储存卡数据。

> **下载版本提示**：
> 请优先从 [GitHub Releases](https://github.com/LyleLiu666/deskpet-platform/releases) 最新发布的同一版本页面获取配套的首刷包、升级包、`SHA256SUMS-firmware.txt` 及源码与重链接材料包。
> - 截至当前公开预览版：[v0.1.0-rc6 下载页](https://github.com/LyleLiu666/deskpet-platform/releases/tag/v0.1.0-rc6) 提供了 `firmware-0834cc7.bin` 升级镜像与重链接材料，暂未单独附带同版本首刷包；
> - 若手里是**全新的空白开发板**，在新一版首刷包发布前，请先从 [v0.1.0-rc1 下载页](https://github.com/LyleLiu666/deskpet-platform/releases/tag/v0.1.0-rc1) 下载并刷入 `deskpet-esp32-2432s028-0.1.0-rc.54-factory.zip`（核对 `SHA256SUMS-firmware-rc.54.txt`），随后再按下方「3. 升级已有固件」将应用区更新至最新 `firmware-<修订>.bin`。

---

## 2. 首次刷机（空白开发板）

1. **连接 USB 数据口并确认串口号**：
   使用支持数据传输的 USB 线连接开发板的数据口，并关闭可能占用该串口的串口监视器或 Agent：
   - **macOS**：在终端对比插拔前后的 `ls /dev/cu.*` 输出，记下新增的串口（如 `/dev/cu.usbserial-140`）。
   - **Windows**：在「设备管理器 → 端口 (COM 和 LPT)」中查看新增的 `COM` 号（如 `COM5`）。
2. **解压并校验 SHA-256**：
   解压首刷包并进入解压目录，确认所有 `.bin` 文件的校验和与包内 `SHA256SUMS` 完全一致后再继续：
   - **macOS**：
     ```sh
     shasum -a 256 -c SHA256SUMS
     ```
   - **Windows（PowerShell）**：
     ```powershell
     Get-ChildItem -Filter *.bin | Get-FileHash -Algorithm SHA256
     ```
3. **准备 `esptool` 刷机环境**：
   确保电脑已安装 Python，在解压目录内创建独立虚拟环境并安装 `esptool==4.5.1`：
   - **macOS**：
     ```sh
     python3 -m venv .flash-venv
     source .flash-venv/bin/activate
     python -m pip install esptool==4.5.1
     ```
   - **Windows（PowerShell）**：
     ```powershell
     py -3 -m venv .flash-venv
     .\.flash-venv\Scripts\python.exe -m pip install esptool==4.5.1
     ```
     *（Windows 执行下方命令时，请将开头的 `python` 替换为 `.\.flash-venv\Scripts\python.exe`）*
4. **识别芯片并刷入完整镜像**：
   将下方命令中的 `PORT` 替换为实际串口号（如 `/dev/cu.usbserial-140` 或 `COM5`）。先运行第一行确认识别到 `ESP32` 和 `4MB` Flash，再运行第二行写入并校验：

```sh
python -m esptool --chip esp32 --port PORT flash_id
python -m esptool --chip esp32 --port PORT write_flash --verify 0x1000 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 firmware.bin
```

---

## 3. 升级已有固件

1. **确认设备并按需备份 Flash**：
   核对目标串口对应的设备身份。如需完整备份当前 4 MiB Flash，可运行：
   ```sh
   python -m esptool --chip esp32 --port PORT read_flash 0x0 0x400000 backup-flash.bin
   ```
   *（注意：完整 Flash 备份中包含已保存的 Wi-Fi 密码与配对密钥，请仅保存在本机私有目录，切勿外传。）*
2. **校验升级包**：
   核对下载的升级包或 `firmware-<修订>.bin` 的 SHA-256 与发布页 `SHA256SUMS-firmware.txt` 完全一致。
3. **校验板端分区表并仅更新 `0x10000` 应用区**：
   先读回板端 `0x8000` 处的 3,072 字节分区表，确认其 SHA-256 为 `9802f3e727b22df3adbaff6b92f93c0bd07fc8b35c7332a90ef13bb57f058f8a`（或与升级包 `manifest.json` / `README.md` 声明的值一致），校验通过后只写入 `0x10000` 应用区：

```sh
python -m esptool --chip esp32 --port PORT read_flash 0x8000 0xc00 partitions-readback.bin
python -c "import hashlib,sys; h=hashlib.sha256(open('partitions-readback.bin','rb').read()).hexdigest(); print(h); sys.exit(h != '9802f3e727b22df3adbaff6b92f93c0bd07fc8b35c7332a90ef13bb57f058f8a')"
python -m esptool --chip esp32 --port PORT write_flash --verify 0x10000 firmware.bin
```

---

## 4. 常见刷机问题与开机引导

- **找不到串口**：请先更换支持数据传输的 USB 线或换用开发板上的数据 USB 口。Windows 设备管理器若识别到 USB 转串口芯片（如 CH340）但未分配 `COM` 号，请先安装对应串口驱动。
- **识别到串口但无法连接芯片**：按开发板说明按住 `BOOT` 键进入下载模式后重试。
- **高速写回校验中断**：部分 CH340 串口在默认高速写入校验时若报错 `Serial data stream stopped`，在 `write_flash` 前加上 `--baud 230400` 降低波特率重试即可稳定通过。
- **刷机完成后首次开机**：
  重启开发板后，首次尚未校准电阻屏时会引导轻触屏幕依次点完三处十字准星，随后自动进入首页。首页右侧会提示「点击连接 Wi-Fi」（联网后未配对时提示「点击配对电脑」），点按即可扫码联网与配对。空 TF 卡开机时使用固件内置菜单与速率面板，连好电脑端后可一键生成并安装 `Pocket Bot` 示例角色。详见[操作手册](../src/deskpet_platform/manuals/operations.md#3-给搭搭配网与配对)。
