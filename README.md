# DeskPet Platform

**搭搭（DeskPet）** 的电脑端连接程序。装好后，桌上的搭搭小屏幕就能和你平时用的编程 Agent（Codex、Claude Code、Antigravity、OpenCode、WorkBuddy 等）联动：

- **看着它干活**：角色动作随实时 Token 输出速度换档，还能顺手看额度、电脑 CPU / 内存、时钟、天气和相册。
- **做完了叫你**：Agent 跑完长任务，或者停下来等你拍板、回答问题时，搭搭会在桌面上弹出提醒。
- **自己换角色**：可以先装内置生成的示例角色体验，也可以用一张参考图让 Agent 帮你定制专属角色。

如果你手里是**已刷好固件的搭搭**，直接往下看开始安装即可；如果是刚买的空白开发板（仅兼容 **ESP32-2432S028 双 USB 版**：ST7789 屏幕、XPT2046 触摸、4 MiB Flash），请先按[刷机说明](docs/flash.md)刷入首刷包。

[交给你的 Agent 安装](#交给你的-agent-安装) · [给搭搭加一个角色](#给搭搭加一个角色) · [手动安装](#手动安装) · [手机扫码打开本页](#开始使用二维码) · [许可证与致谢](#许可证与致谢)

## 交给你的 Agent 安装

首次使用**不需要**自己下载本仓库，也不用记命令：

1. 给搭搭通电（**推荐直接用能传数据的 USB 线插在电脑上**，后面配对最省事；插充电头也可以）。
2. 手机留在手边，等会儿用来扫屏幕二维码给搭搭连 2.4 GHz Wi-Fi。
3. 把下面整段话复制给你平时用的编程 Agent（Windows 用户请在原生 Windows 环境里运行 Agent，不要在 WSL 里装）：

```text
请帮我在这台电脑上安装「搭搭」电脑端，连上我的搭搭小屏幕，并配好当前 Agent 的消息提醒。

先读 https://github.com/LyleLiu666/deskpet-platform 的 README.md 和操作手册
src/deskpet_platform/manuals/operations.md。
全程请用通俗直白的大白话引导我，需要我配合时（比如手机扫码连 Wi-Fi、点一下信任、
看一眼屏幕）每次只让我做一件事，不要抛内部参数名或术语。
保留我现有的 Agent 设置、其他已配对设备和储存卡数据。Windows 请在原生 Windows
环境安装，不要装在 WSL 内。涉及 Wi-Fi 密码、配对码和密钥时，只让我在手机或本机
弹出的网页/终端里输入，绝不要让我发到聊天框里。

1. 下载与安装：
   使用我指定的版本；若未指定，打开 https://github.com/LyleLiu666/deskpet-platform/releases?q=prerelease%3Atrue ，
   从最新的预览版开始，选择有适配本机桌面包的版本。
   告诉我选了哪个版本；如果是预览版，请注明。优先下载与当前系统、芯片架构匹配的「桌面包」
   （自带预编译测速采集器 AgentSpeedProbe），核对同页 SHA256SUMS.txt 后，解压到
   我的用户持久目录（例如 macOS/Linux 用 ~/.local/share/deskpet-platform，
   Windows 用 %LOCALAPPDATA%\Programs\DeskPetPlatform；不要放在系统临时目录或
   下载目录，避免重启或清理后失效）。
   检查本机是否有 Python 3.11–3.14 或 uv，缺少时先告诉我去哪里装。
   macOS 运行 bash ./install-macos.command；
   Windows 运行 powershell -ExecutionPolicy Bypass -File .\install-windows.ps1；
   Linux 按 README「手动安装」用 uv sync 或 python3 -m venv 安装到 .venv。用 --version 确认安装成功。
   把生成的 deskpet-platform 绝对路径（Windows 带 .exe）以 stdio MCP 注册到当前
   Agent：有兼容自带采集器且我不跳过测速时传 mcp；若当前系统没有对应预编译采集器
   （如 Intel Mac、非 glibc Linux、Windows ARM）或我选择跳过测速，传
   mcp --no-token-probe（不要让我装 Swift/.NET SDK 去编译采集器）。
   Codex 请按操作手册把该 MCP 的 tool_timeout_sec 设为 1200。
   如果当前对话还读不到刚注册的 MCP，请清楚告诉我怎么新开一个对话继续第 2 步。

2. 配网与配对：
   用 desk_list_devices 检查是否已有配对（已有则复用对应 device_id，新加另一台搭搭
   时传 add_device=true；空白开发板先按 docs/flash.md 核对板型与首刷包）。
   若搭搭还没连 Wi-Fi，请引导我在搭搭上打开 Wi-Fi 二维码（菜单「桌面设置 → Wi-Fi」
   或参考固件长按屏幕 3 秒），用手机扫左右两个码连上 2.4 GHz Wi-Fi，并确认电脑和
   搭搭在同一个可互访的局域网里。
   配对方式二选一：
   - 如果搭搭正用 USB 数据线插在电脑上，优先用 find-usb（或 desk_find_usb_device）
     确认串口和身份，再调 desk_pair_usb 直接配对，省去手传长配对码（注意 Windows
     不支持通过 USB credentials_file 配网，仍先用手机扫码连 Wi-Fi）。
   - 如果搭搭没插电脑 USB，调 desk_pair_begin 打开本机配对网页，引导我在搭搭
     「桌面设置 → 电脑配对」用手机扫码复制配对码，粘贴到刚才弹出的本机网页里。
   最后用 desk_status 确认已连上。连不上时先问我屏幕显示什么，再按手册排查局域网
   隔离、VPN/代理、macOS 局域网权限或旧固件所需的 Windows 防火墙设置。

3. 开启消息提醒：
   若工具列表中有 desk_notification_setup，先调用它查看各客户端（Codex、Claude Code、
   Antigravity、OpenCode、WorkBuddy、WorkBuddy AI）的提醒支持状态；旧版无此工具时
   按操作手册判断（Windows 目前仅支持 Codex 和 Claude Code 提醒）。
   当前 Agent 受支持时，调用对应工具开启提醒（Codex 调 desk_enable_codex_notifications；
   Claude Code 调 desk_set_claude_notifications(enabled=true)；Antigravity 调
   desk_set_antigravity_notifications(enabled=true)；OpenCode 调
   desk_set_opencode_notifications(enabled=true)；WorkBuddy 按版本调
   desk_set_workbuddy_notifications(enabled=true, edition=...)），并问我要不要顺便
   给电脑上其他常用的受支持 Agent 也打开提醒。若换过安装目录，重新调用一次即可更新旧路径。
   按返回结果用大白话告诉我需要配合的操作（例如 Codex 进 /hooks 或设置里的 Hooks
   信任刚加的搭搭条目；Antigravity 先开新会话验证，未加载时再重启；OpenCode / WorkBuddy
   重启客户端）。
   若当前系统或客户端暂不支持提醒钩子，直接说明原因，不要强行调用报错。

4. 屏幕与测速验收：
   - 若已开启当前 Agent 的提醒：请我新开一个对话随便发句短消息等回复结束，让钩子真实
     触发一次；再帮我用 desk_list_notifications（当前客户端在支持列表内时也可跑
     deskpet-platform doctor --client <客户端>）核对完成事件与设备显示回执，并请我
     看一眼搭搭屏幕，确认跳出了清晰可读、没有方块乱码的中文提醒（手动发文字只算测连接，
     不算钩子验收；待答复提醒等以后实际遇到提问时再看）。若提示设备 busy，提醒我关掉
     占用连接的其他会话。
   - 若当前环境不支持提醒钩子：用 desk_show_text 发一句中文测试文字，请我确认屏幕上
     中文显示正常、没有乱码方块。
   - 若启用了测速：调用 desk_token_status 确认测速状态正常。
   - 顺手看一下 desk_status：如果储存卡上还没有装过角色，告诉我可以一句话先装一个
     Pocket Bot 示例角色动起来；如果我想看额度，按操作手册配置并教我在搭搭「监控设置」切换。

5. 收尾汇总：
   最后用好懂的话分别告诉我「设备连接」「完成提醒」「待答复提醒」「实时测速」四项的结果：
   标明「已验证」（附依据）、「已配置待确认」（还差哪一步）或「未启用」（原因及日后开启方式）。
```

## 给搭搭加一个角色

搭搭固件出厂不预装角色素材。连好 MCP 之后，直接用日常说话的方式吩咐你的 Agent 就行：

- **想先看它动起来**：「帮我在搭搭上装一个 Pocket Bot 示例角色，我想在图鉴里看看效果。」（无需准备图片，几秒生成并传到储存卡）
- **想做自己的角色**：「照着我发的这张图做一只奶油色小猫，平时眨眼、点它挥爪，先在电脑上给我看一段跑动预览。」
- **已有现成角色包**：「帮我把下载文件夹里的角色 ZIP 装到搭搭上。」

更多对话示例、制作尺寸与验收要求见[自定义角色指南](src/deskpet_platform/manuals/roles.md)（Agent 也可直接调用 `desk_read_manual(topic="roles")` 读取）。

## 手动安装

如果你不想让 Agent 代装，可以按下面五步手动完成：

### 1. 下载桌面包并解压到固定目录

打开 [电脑端预览版下载](https://github.com/LyleLiu666/deskpet-platform/releases?q=prerelease%3Atrue)，在最新预览版下展开 **Assets（附件）**，按下表下载与你的系统和芯片匹配的桌面包。核对同一版本的 `SHA256SUMS.txt`，再解压到一个**长期保留的目录**。

| 系统 | 桌面包文件名（`<版本>` 以所选预览版为准） |
| --- | --- |
| macOS 14 或更新（Apple 芯片） | `deskpet_platform-<版本>-macos-arm64.tar.gz` |
| Windows x64（原生环境，非 WSL） | `deskpet_platform-<版本>-windows-x64.zip` |
| Linux x64（glibc） | `deskpet_platform-<版本>-linux-x64.tar.gz` |
| Linux arm64（glibc） | `deskpet_platform-<版本>-linux-arm64.tar.gz` |

> **关于测速采集器**：上表的桌面包已内置预编译的 `AgentSpeedProbe`，装好后直接用 `mcp` 启动即可测速。如果你是直接 `git clone` 本仓库，或下载了通用源码包 / `.whl` 文件，里面**不含**预编译采集器——启动时可加 `mcp --no-token-probe` 关闭测速（配对和消息提醒照常使用），或用 `mcp --token-probe <采集器绝对路径>` 指定独立采集器。设备配置目录 `bin/` 内放有采集器时优先于随包版本（Windows 查找 `AgentSpeedProbe.exe` 和 `AgentSpeed.Probe.exe`，macOS 和 Linux 查找 `AgentSpeedProbe`）。

### 2. 安装运行环境

请先确保电脑已安装 **Python 3.11–3.14** 或 **uv**（[macOS 下载 Python](https://www.python.org/downloads/macos/) · [Windows 下载 Python](https://www.python.org/downloads/windows/)），然后在解压后的目录中执行：

- **macOS**（终端）：
  ```sh
  bash ./install-macos.command
  ```
  安装完成后，程序位于目录内的 `.venv/bin/deskpet-platform`。

- **Windows**（PowerShell）：
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
  ```
  安装完成后，程序位于目录内的 `.venv\Scripts\deskpet-platform.exe`。

- **Linux**（终端；USB 配对与实机连接仍待验证）：
  - 已安装 `uv` 时直接运行：
    ```sh
    UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --locked --no-editable --python ">=3.11,<3.15"
    ```
  - 使用系统 Python 3.11–3.14 时（需已按发行版说明安装 `venv` 组件）：
    ```sh
    python3 -m venv .venv
    .venv/bin/python -m pip install .
    ```
    如果使用本地 `.whl` 包，把 `pip install .` 换成 `pip install /绝对路径/deskpet_platform-<版本>-py3-none-any.whl` 即可。
  安装完成后，程序位于目录内的 `.venv/bin/deskpet-platform`。

### 3. 注册到你的 Agent（MCP）

将上面安装得到的 `deskpet-platform` **绝对路径**（Windows 带 `.exe`）和 `mcp` 参数写入客户端的 stdio MCP 配置。例如 Codex 和 Claude Code 可直接运行：

```sh
codex mcp add deskpet -- "/绝对路径/.venv/bin/deskpet-platform" mcp
claude mcp add --scope user deskpet -- "/绝对路径/.venv/bin/deskpet-platform" mcp
```

其他支持 stdio MCP 的客户端（如 Antigravity、OpenCode、WorkBuddy 等）使用相同的可执行文件绝对路径与 `["mcp"]` 参数（跳过测速时加 `"--no-token-probe"`）。设置完成后**新开一个 Agent 会话**使其生效。

### 4. 给搭搭联网并完成配对

1. **连 Wi-Fi**：给搭搭通电，打开屏幕上的 Wi-Fi 配网二维码（菜单「桌面设置 → Wi-Fi」，参考固件长按屏幕 3 秒），用手机先扫左码连热点、再扫右码选 2.4 GHz Wi-Fi 填密码。完成后让电脑和搭搭处在同一局域网。
2. **配对连接**（一台搭搭同一时间只能被一个程序连接）：
   - **插着电脑 USB 数据线时**：用 MCP 的 `desk_find_usb_device`（或终端 `deskpet-platform find-usb <串口>`）确认串口与设备身份，再调 `desk_pair_usb` 即可配对（macOS / Linux 还可通过 `0600` 私有凭据文件同一步配网）。
   - **无线供电时**：在 Agent 里调用 `desk_pair_begin` 打开本机配对网页（或在终端运行 `deskpet-platform pair`），到搭搭「桌面设置 → 电脑配对」用手机扫码复制配对码，粘贴到本机完成验证。
3. **验证连接与测速**：在 Agent 中调用 `desk_status` 检查连接，跑一次对话后可用 `desk_token_status` 查看测速读数。支持的固件还可开启电脑 CPU / 内存使用率显示（默认关闭），或用 `desk_list_role_sources`、`desk_check_role`、`desk_install_role` 检查并安装本地角色包。

### 5. 开启消息提醒并在新会话验证

1. **打开提醒开关**：在已接入 MCP 的会话中调用 `desk_notification_setup` 查看当前系统支持的客户端，再调用对应的启用工具：
   - Codex：`desk_enable_codex_notifications`
   - Claude Code：`desk_set_claude_notifications(enabled=true)`
   - Antigravity / OpenCode / WorkBuddy（macOS 与 Linux）：分别调用 `desk_set_antigravity_notifications(enabled=true)`、`desk_set_opencode_notifications(enabled=true)`、`desk_set_workbuddy_notifications(enabled=true, edition=...)`（Windows 目前仅支持 Codex 和 Claude Code 提醒）。
2. **完成信任或加载**：Codex 需进入 `/hooks` 或设置中的 Hooks 审核并信任新增条目；Claude Code 与 Antigravity 直接打开新会话验证（Antigravity 若新会话未加载再重启客户端）；OpenCode 与 WorkBuddy 需重启客户端。
3. **验证屏幕提醒**：**新开一个会话**发一句短消息等回复结束，确认搭搭屏幕上弹出清晰可读的中文完成提醒（也可用 `desk_list_notifications` 或 `deskpet-platform doctor --client <客户端>` 核对回执；待答复提醒在后续实际触发提问时单独验收）。

### 常用文档与示例

- [操作手册（配网排障、多台设备、各客户端提醒钩子与额度设置）](src/deskpet_platform/manuals/operations.md)
- [自定义角色指南](src/deskpet_platform/manuals/roles.md) · [角色包格式与动作接口](docs/role-format.md)
- [兼容板型与首次刷机说明](docs/flash.md)
- [二次开发手册（Python SDK 与局域网协议）](src/deskpet_platform/manuals/development.md) · [状态查询示例](examples/status.py) · [屏幕点击回传示例](examples/tap.py)

## 开始使用二维码

手机扫码可在手机上查看本页安装说明；在电脑上打开同一链接并把安装提示词复制给你的 Agent，即可配合手机完成配网和配对。[打开安装步骤](https://github.com/LyleLiu666/deskpet-platform#交给你的-agent-安装)

<img src="https://cdn.jsdelivr.net/gh/LyleLiu666/deskpet-platform@main/docs/assets/start-qr.png" width="240" height="240" alt="DeskPet 安装步骤二维码">

[打开二维码原图](https://cdn.jsdelivr.net/gh/LyleLiu666/deskpet-platform@main/docs/assets/start-qr.png)

## 许可证与致谢

- **DeskPet 许可证**：DeskPet 自有软件采用 [PolyForm Noncommercial 1.0.0](LICENSE) 许可证，允许非商业用途的使用、修改和分发；商业使用需另行获得授权。随附的客户端图标沿用[各自的许可证](src/deskpet_platform/assets/agent_icons/LICENSE)。
- **Agent Speed 致谢**：本项目当前使用 [zoefix/agent-speed](https://github.com/zoefix/agent-speed/tree/29dffc4e6fbe1da435071e40781b1571dd45ac22) 的 `AgentSpeedProbe` 进行实时 Token 测速，并基于其 Swift 实现开发 Python 移植版（保留原项目的 MIT 许可证和版权声明；Python 移植版增加了测试与部分修复，目前仍在验证中）。感谢原项目作者的工作。
