# DeskPet Platform

独立的 DeskPet 连接底座。提供局域网 SDK、MCP 和可运行示例。

DeskPet 自有软件采用 [PolyForm Noncommercial 1.0.0](LICENSE)，允许非商用使用、修改和分发。商用需要另行获得授权。随附的图标沿用[各自的许可证](src/deskpet_platform/assets/agent_icons/LICENSE)。

本仓库提供电脑端源码。兼容的开发板是 **ESP32-2432S028 双 USB 版**（ST7789 屏幕、XPT2046 触摸、4 MiB Flash）。已预刷兼容固件的设备可直接配对。板端二进制包的发布状态见[刷机说明](docs/flash.md)。

[交给你的 Agent 安装](#交给你的-agent-安装) · [手动安装](#手动安装) · [开始使用二维码](#开始使用二维码)

## Agent Speed 致谢

本项目目前使用 Agent Speed 的 AgentSpeedProbe，并在开发其 Python 移植版。该移植基于 zoefix/agent-speed 的 Swift 实现，保留原项目的 MIT 许可证和版权声明。DeskPet Platform 提供设备接入；Python 移植版增加了测试和部分修复，目前仍在验证。感谢原项目作者的工作。

## 交给你的 Agent 安装

把下面这段复制给你平时使用的 Agent，在本仓库目录里开始。

```text
请帮我在这台电脑上安装和配置 DeskPet，并只操作我选中的小板。

1. 按本仓库 README 和操作手册安装 DeskPet Platform。macOS 优先用
   install-macos.command。把安装后的 deskpet-platform 绝对路径注册为
   本机 stdio MCP，参数为 mcp，保留我已有的 MCP 配置。使用 Codex 时，
   按操作手册设置 DeskPet 的写卡等待时间。
2. 先用 desk_list_devices 查看已配对设备。已有这台小板时复用配对；
   添加另一台时使用 add_device=true。通过 USB 配对前，用已安装的
   deskpet-platform find-usb 查询明确指定的候选串口，核对小板的 UUID
   和 MAC。已有本机私有 Wi-Fi 凭据文件时可用 desk_pair_usb 同时配网。
   如果这台小板已经配对，传入列表里对应的 device_id。
   需要手动配网时，请让我用手机扫小板上的两个配网码，在手机上输入
   Wi-Fi 密码。小板联网后，用 desk_pair_begin 打开电脑配对页，请我
   将小板上的配对码填入本机页面。
3. 如果我已安装 Agent Speed，优先复用现有可执行的 AgentSpeedProbe，
   用 --token-probe 指定其绝对路径。如果我希望跳过测速，给 MCP 加
   --no-token-probe，继续完成 DeskPet 安装和配对。找不到采集程序时先告诉我，
   由我决定是否安装。请勿在聊天中显示 Wi-Fi 密码、配对码或密钥。
4. 用 desk_list_devices 确认目标设备 ID，再用 desk_status 验证连接。
   配置了 Agent Speed 时，用 desk_token_status 检查是否收到有效读数。
   告诉我已验证的结果，以及还需要我在小板或手机上完成的步骤。
5. 如果我希望接收提醒，按操作手册为这台小板启用我指定的 Agent。
   需要在 Agent 界面里审核钩子时，请告诉我到哪里操作。用一个新会话
   验证小板收到提醒。
```

## 给小板加一个角色

接好 MCP 后，可以直接对 Agent 说「帮我装一个 Pocket Bot 示例角色」，体验储存卡安装和图鉴试播。想定制角色，可以从参考图和一段动作预览开始，例如「按这张图设计一只小猫，先让我看看它怎么跑」。定稿后按角色标准补齐素材，再打包安装。

更多对话示例和制作步骤见[自定义角色指南](src/deskpet_platform/manuals/roles.md)。Agent 也可以通过 `desk_read_manual(topic="roles")` 读取。

## 手动安装

在仓库目录或解压后的 Python 源码包目录中，macOS 可运行 `./install-macos.command`。安装使用已有的 uv 或 Python 3.11 以上版本。需要时可从官网下载。[macOS 下载](https://www.python.org/downloads/macos/)

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/deskpet-platform pair
.venv/bin/deskpet-platform status
```

已有本地 wheel 文件时，可将上面的 `pip install .` 改为 `pip install /绝对路径/deskpet_platform-0.1.0-py3-none-any.whl`。

用 MCP 的 `desk_pair_usb` 配对已登记的小板。提供本机私有 Wi-Fi 凭据文件时，同一步也可完成配网。需要手动设置时，可扫描小板上的配网二维码，再用 `desk_pair_begin` 打开本机配对页。一块小板同一时间由一套服务连接。

在 MCP 客户端中以 stdio 注册 `.venv/bin/deskpet-platform mcp`，配置时使用绝对路径。设备支持时钟、相册、天气、Token 状态、Codex 消息和可插拔角色。操作与开发手册也可通过 MCP 读取。

Token 速度使用原版 `AgentSpeedProbe`。在设备配置目录的 `bin/AgentSpeedProbe` 放入采集程序，或启动 MCP 时指定 `--token-probe <程序绝对路径>`。使用 `mcp --no-token-probe` 可跳过测速，即使配置目录中已有采集程序。采集程序的源码、许可证和构建说明见 [Agent Speed](https://github.com/zoefix/agent-speed/tree/29dffc4e6fbe1da435071e40781b1571dd45ac22)。支持的设备还可分别开启电脑 CPU 和内存使用率，默认关闭。

固件不预装角色。可将自制角色包存放在本机目录。用 `desk_list_role_sources` 查找素材，用 `desk_check_role` 检查，再用 `desk_install_role` 传到储存卡。设备校验完整后启用。

- [操作手册](src/deskpet_platform/manuals/operations.md)
- [二次开发手册](src/deskpet_platform/manuals/development.md)
- [SDK 示例](examples/status.py) · [设备点击回传](examples/tap.py)
- [兼容板型和刷机说明](docs/flash.md)
- [角色包与动作接口](docs/role-format.md)

## 开始使用二维码

手机扫码可打开本页的安装步骤。在电脑上打开同一链接并完成安装，再按小板上的提示配网和配对。[打开安装步骤](https://github.com/LyleLiu666/deskpet-platform#交给你的-agent-安装)

<img src="docs/assets/start-qr.png" width="240" height="240" alt="DeskPet 安装步骤二维码">

[下载二维码原图](docs/assets/start-qr.png)
