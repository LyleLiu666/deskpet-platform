# DeskPet Platform

独立的 DeskPet 连接底座。提供局域网 SDK、MCP 和可运行示例。

DeskPet 自有软件采用 [PolyForm Noncommercial 1.0.0](LICENSE)，允许非商用使用、修改和分发。商用需要另行获得授权。随附的图标沿用[各自的许可证](src/deskpet_platform/assets/agent_icons/LICENSE)。

本仓库提供电脑端源码。兼容的开发板是 **ESP32-2432S028 双 USB 版**（ST7789 屏幕、XPT2046 触摸、4 MiB Flash）。已预刷兼容固件的搭搭可直接配网和配对。空白板需要单独取得对应板型的固件首刷包，再按[刷机说明](docs/flash.md)操作。

[交给你的 Agent 安装](#交给你的-agent-安装) · [手动安装](#手动安装) · [开始使用二维码](#开始使用二维码)

## Agent Speed 致谢

本项目目前使用 Agent Speed 的 AgentSpeedProbe，并在开发其 Python 移植版。该移植基于 zoefix/agent-speed 的 Swift 实现，保留原项目的 MIT 许可证和版权声明。DeskPet Platform 提供设备接入；Python 移植版增加了测试和部分修复，目前仍在验证。感谢原项目作者的工作。

## 交给你的 Agent 安装

打开 [DeskPet Platform 的 GitHub 页面](https://github.com/LyleLiu666/deskpet-platform)，把下面这段复制给你平时使用的 Agent。首次使用不需要预先下载仓库。

```text
请帮我在这台电脑上安装搭搭，连上我选中的搭搭，并启用当前 Agent 的提醒。

先读 https://github.com/LyleLiu666/deskpet-platform 的 README 和操作手册。
用我收到的 Release 链接或版本。若未指定，选最新的稳定版；只有预览版时，
告诉我正在使用哪个预览版。保留已有 Agent 设置、其他设备的配对和储存卡数据。
开始前确认系统、硬件架构、当前客户端、可用的采集器包和钩子支持情况。
告诉我需要配合的步骤，例如手机配网、审核钩子和看屏幕。

1. 优先选 Release 中与系统和架构匹配的桌面包，核对 SHA256SUMS.txt。
   在独立目录解压安装。macOS 用 install-macos.command，Windows 用
   install-windows.ps1，Linux 按「手动安装」使用 Python 虚拟环境。
   用 --version 确认安装版本。带入旧版时可用 --help 检查入口。
   把 deskpet-platform 的绝对路径
   （Windows 带 .exe）和 mcp 参数加入当前 Agent 的
   stdio MCP 配置，首次启动前按第 5 步准备好测速参数。按操作手册设置
   Codex 的写卡等待时间。如果当前会话
   读不到新增 MCP，告诉我如何开启新会话并续做配对和提醒。
2. 用 desk_list_devices 查找已有配对。复用对应的 device_id，新增设备
   时使用 add_device=true。若走 USB，先用 find-usb 核对搭搭的 UUID 和 MAC，
   再用 desk_pair_usb。已有本机私有 Wi-Fi 凭据文件时，可同一步配网。
   手动配网时，请我用手机扫搭搭的两个配网码，在手机上填 Wi-Fi 密码。
   搭搭联网后，用 desk_pair_begin 打开配对页，请我在页面填搭搭显示的
   配对码。空白设备先按刷机说明核对板型与首刷包。用 desk_status 核实
   选中的搭搭已连接。配网卡住时，先问我搭搭屏幕显示到哪一步，再按手册排查。
   如果搭搭显示已联网但 Windows 未连上，先核对两端在同一可互访的局域网，
   再按手册检查旧版回连所需的防火墙设置。
   涉及密码、配对码和密钥时，只在对应的本机界面使用。
3. 配对后，直接调用当前 Agent 对应的提醒工具。Codex 调用
   desk_enable_codex_notifications；Claude Code 调用
   desk_set_claude_notifications(enabled=true)；Antigravity 调用
   desk_set_antigravity_notifications(enabled=true)；OpenCode 调用
   desk_set_opencode_notifications(enabled=true)；WorkBuddy 调用
   desk_set_workbuddy_notifications(enabled=true)，按客户端选 edition。
   工具会为这套配置内的已配对设备启用提醒。按工具结果完成审核或重启。
   Codex 请明确告诉我进入 /hooks 或设置中的 Hooks，审核并信任刚写入的
   搭搭条目。若系统或客户端不支持钩子，告诉我具体原因。
4. 用新会话完成一个短任务，让真实钩子触发一次。用 desk_list_notifications
   查目标搭搭在这个任务开始后收到的完成事件。新版可用 doctor 核对这条
   新提醒的设备显示回执。更换电脑端安装目录后，重新执行第 3 步。
   请我确认这次任务的提醒出现在搭搭屏幕上，中文可读且没有方块。
   手动发文字只能验证连接。待答复提醒有实际触发时再验收。
   若旧会话占用搭搭，请我关闭对应会话。
   新会话可以先运行 deskpet-platform doctor --client <当前客户端> 查看安装
   进度。按实际配置传入 --config（放在 doctor 前）、--device-id 或 --shared-config。
5. 测速可选。桌面包自带采集器，启用测速时直接用 mcp 启动。用
   desk_token_status 验证读数。如果我选择跳过测速，用 mcp --no-token-probe。
   只用预编译采集器。无需下载 Swift 或 .NET SDK，也无需编译采集器源码。
   当前系统没有兼容采集器时，用 mcp --no-token-probe 继续配对和提醒。
6. 分别汇报配对、完成提醒、待答复提醒和测速。用「已验证」附证据，
   「已配置但未验证」写缺少的确认，「未启用」说明原因和重试方法。
   我确认屏幕上出现真实完成提醒后，才算完成安装验收。
   若我需要额度显示，按操作手册设置，再从搭搭的「监控设置」切换。
```

## 给搭搭加一个角色

接好 MCP 后，可以直接对 Agent 说「帮我装一个 Pocket Bot 示例角色」，体验储存卡安装和图鉴试播。想定制角色，可以从参考图和一段动作预览开始，例如「按这张图设计一只小猫，先让我看看它怎么跑」。定稿后按角色标准补齐素材，再打包安装。

更多对话示例和制作步骤见[自定义角色指南](src/deskpet_platform/manuals/roles.md)。Agent 也可以通过 `desk_read_manual(topic="roles")` 读取。

## 手动安装

在仓库目录或解压后的源码包目录中，macOS 在终端运行 `bash ./install-macos.command`，Windows 在 PowerShell 中运行 `powershell -ExecutionPolicy Bypass -File .\install-windows.ps1`。安装使用已有的 uv 或 Python 3.11 至 3.14。Linux 使用下面的 Python 虚拟环境命令，USB 配对和实机连接仍待验证。[macOS 下载](https://www.python.org/downloads/macos/) · [Windows 下载](https://www.python.org/downloads/windows/)

Windows 安装完成后，程序位于 `.venv\Scripts\deskpet-platform.exe`；MCP 使用该程序的绝对路径和 `mcp` 参数。下列命令适用于 macOS 和 Linux。Linux 若缺少 venv，请按发行版说明安装 Python 的 venv 组件。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/deskpet-platform pair
.venv/bin/deskpet-platform status
```

已有本地 wheel 文件时，可将上面的 `pip install .` 改为 `pip install /绝对路径/deskpet_platform-0.1.0rc5-py3-none-any.whl`。

用 MCP 的 `desk_pair_usb` 配对已登记的搭搭。提供本机私有 Wi-Fi 凭据文件时，同一步也可完成配网。需要手动设置时，可扫描搭搭上的配网二维码，再用 `desk_pair_begin` 打开本机配对页。一台搭搭同一时间由一套服务连接。

在 MCP 客户端中以 stdio 注册 `.venv/bin/deskpet-platform mcp`，配置时使用绝对路径。设备支持时钟、相册、天气、Token 状态、Codex 消息和可插拔角色。操作与开发手册也可通过 MCP 读取。

在 [搭搭 Release](https://github.com/LyleLiu666/deskpet-platform/releases) 选择对应系统的桌面包。桌面包自带 `AgentSpeedProbe`，安装后用 `mcp` 启动即可识别。安装方法与源码包相同。

| 系统 | RC5 桌面包 |
| --- | --- |
| macOS 14 或更新，Apple 芯片 | `deskpet_platform-0.1.0rc5-macos-arm64.tar.gz` |
| Windows x64 | `deskpet_platform-0.1.0rc5-windows-x64.zip` |
| Linux x64（glibc） | `deskpet_platform-0.1.0rc5-linux-x64.tar.gz` |
| Linux arm64（glibc） | `deskpet_platform-0.1.0rc5-linux-arm64.tar.gz` |

测速可用 `mcp --no-token-probe` 关闭。使用通用包时，可以继续配对和接收提醒，也可用 `mcp --token-probe <独立采集器的绝对路径>` 启用测速。设备配置目录 `bin/` 内已装的采集器优先于随包版本。Windows 查找 `AgentSpeedProbe.exe` 和 `AgentSpeed.Probe.exe`，macOS 和 Linux 查找 `AgentSpeedProbe`。运行一个任务后用 `desk_token_status` 确认读数。采集器保留 [Agent Speed 源码链接](https://github.com/zoefix/agent-speed/tree/29dffc4e6fbe1da435071e40781b1571dd45ac22) 和许可证。

支持的设备还可分别开启电脑 CPU 和内存使用率，默认关闭。

固件不预装角色。可将自制角色包存放在本机目录。用 `desk_list_role_sources` 查找素材，用 `desk_check_role` 检查，再用 `desk_install_role` 传到储存卡。设备校验完整后启用。

- [操作手册](src/deskpet_platform/manuals/operations.md)
- [二次开发手册](src/deskpet_platform/manuals/development.md)
- [SDK 示例](examples/status.py) · [设备点击回传](examples/tap.py)
- [兼容板型和刷机说明](docs/flash.md)
- [角色包与动作接口](docs/role-format.md)

## 开始使用二维码

手机扫码可打开本页的安装步骤。在电脑上打开同一链接并完成安装，再按搭搭上的提示配网和配对。[打开安装步骤](https://github.com/LyleLiu666/deskpet-platform#交给你的-agent-安装)

<img src="docs/assets/start-qr.png" width="240" height="240" alt="DeskPet 安装步骤二维码">

[下载二维码原图](docs/assets/start-qr.png)
