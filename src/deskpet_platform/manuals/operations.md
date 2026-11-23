# DeskPet 操作手册

本手册涵盖托肯（DeskPet）电脑端的首次配网配对、各 Agent 消息提醒接入、实时测速与额度配置、日常屏幕控制及排障方法。Agent 也可在任意时刻通过 `desk_read_manual` 查阅本手册（`operations`）、二次开发手册（`development`）和自定义角色指南（`roles`）。

---

## 首次连接与配对

### 1. 安装与在 Agent 中注册 MCP

1. **解压到持久目录**：将对应系统的桌面包解压安装到长期保留的用户目录（例如 macOS / Linux 使用 `~/.local/share/deskpet-platform`，Windows 使用 `%LOCALAPPDATA%\Programs\DeskPetPlatform`；不要放在系统临时目录或定期清理的下载目录。Windows 请在原生 Windows 环境安装，不要装在 WSL 内）。
2. **准备 Python 环境**：确保已安装 Python 3.11–3.14 或 `uv`（[macOS Python 下载](https://www.python.org/downloads/macos/) · [Windows Python 下载](https://www.python.org/downloads/windows/)），运行安装脚本生成 `.venv`。
3. **注册 stdio MCP**：将安装后的 `deskpet-platform` 可执行文件**绝对路径**（Windows 为 `.venv\Scripts\deskpet-platform.exe`，路径含空格时需保留引号）注册到你的 Agent。若已有同名 `deskpet` 条目，先检查并更新该条目，切勿覆盖用户的其他 MCP 配置：

```sh
codex mcp add deskpet -- "/安装路径/deskpet-platform" mcp
claude mcp add --scope user deskpet -- "/安装路径/deskpet-platform" mcp
```

- **跳过测速参数**：当系统没有匹配的预编译测速采集器，或用户选择跳过 Token 测速时，在 `mcp` 后加上 `--no-token-probe`（不会替用户额外安装编译工具）。
- **其他 MCP 客户端**：Antigravity、OpenCode、WorkBuddy 等支持 stdio MCP 的客户端，在各自配置文件中填入相同的可执行文件绝对路径与参数数组（`["mcp"]` 或 `["mcp", "--no-token-probe"]`）。
- **指定角色素材目录（可选）**：可在 `mcp` 后增加 `--role-dir <角色素材目录的绝对路径>`，供 `desk_list_role_sources` 默认扫描。
- 配置完成后，**打开新的 Agent 会话**并调用 `desk_list_devices` 验证 MCP 已生效。

### 2. Codex 的写卡等待时间（`tool_timeout_sec`）

通过 Wi-Fi 向托肯储存卡传输角色包或大文件（单文件最大可达 8 MiB）耗时较长。使用 Codex 时，请在 Codex 的 `config.toml` 中为 `deskpet` 的 MCP 条目设置 1,200 秒超时（已有表项时在原表中补充该字段），设置后从新会话连接：

```toml
[mcp_servers.deskpet]
command = "/安装路径/deskpet-platform"
args = ["mcp"]
tool_timeout_sec = 1200
```

大文件或批量安装角色时，其他客户端也应按文件大小相应延长工具调用超时时间。

### 3. 给托肯配网与配对

1. **手机扫码连 2.4 GHz Wi-Fi**：
   - 给托肯通电。首次尚未校准电阻触屏的设备会先引导轻触三个十字准星，完成后进入首页。
   - 尚未保存 Wi-Fi 时，托肯首页右侧会直接提示 **点击连接 Wi-Fi**，轻触即可打开配网二维码（也可从应用菜单进入 **桌面设置 → Wi-Fi**；旧版/参考固件在未显示二维码时可长按屏幕 3 秒打开）。
   - 用手机先扫**左侧二维码**加入托肯热点，再扫**右侧二维码**打开配网页面，选择 2.4 GHz Wi-Fi 并输入密码（列表找不到时可手动输入网络名称）。联网后，请让电脑与托肯处于同一局域网。
2. **与电脑配对（二选一）**：
   - **方式 A：通过 USB 数据线直接配对（推荐）**
     若用支持数据传输的 USB 线将托肯插在电脑上，先运行 `deskpet-platform find-usb <串口>` 或调用 `desk_find_usb_device` 读取并核对该串口上的设备 MAC 与持久 UUID，再调用 `desk_pair_usb`（`port` 填已确认的串口，`expected_identity` 填核对一致的设备 MAC）。
     - Windows 请先用手机扫码连好 Wi-Fi，再调 `desk_pair_usb` 配对；
     - macOS 和 Linux 还可在 `credentials_file` 中传入本机私有 Wi-Fi 凭据文件绝对路径，一步完成配网与配对（文件权限须为 `0600`，内容为包含 `ssid` 和 `password` 两个字符串的 JSON，开放网络密码设为空字符串；若 USB 配网失败，托肯会尝试恢复原有网络）。
     - 托肯已连 Wi-Fi 但电脑局域网暂时无法自动发现时，也可先通过 USB 完成配对。
   - **方式 B：无线扫码配对**
     若托肯未插电脑 USB 数据线，调用 `desk_pair_begin` 打开电脑本机配对网页（或在终端运行 `deskpet-platform pair` 将配对码粘贴到隐藏输入框）。
     - 在已连 Wi-Fi 且未配对的托肯首页点击 **点击配对电脑**（或进入 **桌面设置 → 电脑配对**；旧版/参考固件轻触屏幕显示配对码）。
     - 用手机扫描托肯屏幕上的配对码二维码并复制，粘贴到电脑弹出的本机配对网页中完成验证（切勿将配对码发到 Agent 聊天框）。验证成功后，从首页打开的配对页会自动返回。
     - 调用 `desk_pair_status` 查看配对结果。
3. **验证连接与中文显示**：
   调用 `desk_status` 检查连接状态，再调用 `desk_show_text` 发送短文字；返回 `shown` 或 `finished` 表示设备已实际绘制。托肯电脑端自带思源黑体同源字体 Noto Sans CJK SC，系统缺少常用中文字体时会自动使用随包字体。验收时请确认屏幕上中文清晰可读、无乱码方块。

### 4. 连接排障

- **局域网自动发现失败**：在电脑本机配对页中手动填入托肯屏幕上显示的局域网 IP 地址，重新输入配对码重试。请排查访客 Wi-Fi、路由器客户端（AP）隔离、VPN 或代理是否阻断了局域网互通。
- **防火墙与系统局域网权限**：
  - 当前固件支持由电脑主动向托肯发起连接；旧版固件仍需由托肯回连电脑，Windows 上请检查防火墙是否允许安装目录下的 `.venv\Scripts\python.exe` 在当前专用/受信任网络接收入站连接。
  - macOS 上若 `desk_status` 返回 `local_network_unreachable`，请检查启动 MCP 或常驻服务的客户端/终端是否已在「系统设置 → 隐私与安全性 → 本地网络」中获授权限；插着 USB 数据线时也可传入 `--usb-port <串口>` 或调用 `desk_set_usb_fallback` 继续控制和写卡。
- **状态显示 `busy` 或 `offline`**：
  - 一台托肯同一时间只能被一个控制端连接。显示 `busy` 时，请关闭占用同一台托肯的其他 MCP 会话或后台进程（在终端独立运行 `deskpet-platform status` 也会临时占用连接）。
  - 显示 `offline` 时，请先检查托肯供电、Wi-Fi 状态和配对记录。
  - 若当前 MCP 观察到连接中断，`desk_status` 的 `last_link_loss` 会记录首个错误、连接恢复耗时（毫秒）以及有新读数送达时的测速恢复耗时。
- **更换电脑或解绑**：更换电脑前请先确认上一台电脑已断开连接。删除电脑本机凭据会停止本机自动连接；在托肯端撤销授权请按对应固件菜单操作。

### 5. 多台托肯与独立提醒接收端

- **配对多台托肯**：
  - 添加第二台及更多托肯时，在 `desk_pair_usb` 或 `desk_pair_begin` 中传入 `add_device=true`（终端使用 `deskpet-platform pair --add-device`），不会覆盖已有设备的配对记录。
  - 调用 `desk_list_devices` 可查看本机所有已配对设备（新固件使用设备持久 UUID 作为 `id`；旧固件暂显示 `mac:` 开头标识，升级固件并成功连接后会自动升级为 UUID）。
  - 只有一台设备时无需指定 `device_id`；配对了多台设备时，查询和控制工具需传入目标设备的 `device_id`（取自列表中的 `id`，未传时会提示选择；终端独立查询使用 `deskpet-platform status --device-id <id>`）。
  - 给已配对的托肯通过 USB 重新配置 Wi-Fi 时，在 `desk_pair_usb` 中同时传入该设备的 `device_id`、已确认的串口和 MAC。
- **登记独立提醒接收端（`receiver-add-usb`）**：
  - 如果已有默认托肯，还可用 `receiver-add-usb` 将另一台托肯登记为独立提醒接收端：它不会改变默认设备，且独立保存自己的消息已读状态（登记时保留旧消息及原有已读状态，不会重新弹出旧提醒；此后消息与已读状态由各设备独立管理）。
  - 新设备的 MCP 启动时用 `--shared-config` 指向原设备配置，即可共用原提醒和额度来源。

---

## 消息提醒与各客户端配置

### 各客户端提醒启用方式

每种 Agent 在电脑上启用一次，提醒就会投递给当前配置下所有已配对的托肯。初次配置或新增客户端时，可先调用 `desk_notification_setup` 查看各客户端的支持状态与启用工具：

| 客户端 | 启用 / 管理工具 | 启用后生效方式与平台说明 |
| --- | --- | --- |
| **Codex** | `desk_enable_codex_notifications`（停用：`desk_disable_codex_notifications`） | 启用后进入 Codex 的 `/hooks` 或设置中的 Hooks 审核并信任新增条目，再开新会话验证。支持 macOS / Linux / Windows。设置 `CODEX_HOME` 环境变量后，启用与停用均作用于该目录。[Codex Hooks 说明](https://developers.openai.com/codex/hooks) |
| **Claude Code** | `desk_set_claude_notifications(enabled=true/false)` | 直接打开新的 Claude Code 会话验证。支持 macOS / Linux / Windows。按 `Esc` 取消的提问会在下次发送消息或完成一轮对话时自动清除。[Claude Code Hooks 说明](https://code.claude.com/docs/en/hooks) |
| **Antigravity** | `desk_set_antigravity_notifications(enabled=true/false)` | 先开新会话确认屏幕提醒，若未加载再重启客户端。支持 macOS / Linux（Windows 暂未启用）。[Antigravity Hooks 说明](https://antigravity.google/docs/hooks) |
| **OpenCode** | `desk_set_opencode_notifications(enabled=true/false)` | 启用或升级插件后**重启 OpenCode**。中止或删除会话会清除该会话的待应答状态并保留历史记录。支持 macOS / Linux（Windows 暂未启用）。[OpenCode V1 插件配置](https://opencode.ai/v1/docs/plugins/) |
| **WorkBuddy / WorkBuddy AI** | `desk_set_workbuddy_notifications(enabled=true/false, edition="workbuddy" \| "workbuddy_ai")` | 启用后**重启对应的 WorkBuddy 应用**。提问按请求编号清除，授权请求按会话轮次合并并在该轮结束时清除。支持 macOS / Linux（Windows 暂未启用）。 |

- **更换安装目录后更新钩子**：如果移动了托肯电脑端的安装目录，重新调用一次对应客户端的启用工具即可自动更新旧钩子路径（Codex 更新后需重新在 `/hooks` 审核信任）。
- **真实提醒验收与 `doctor` 诊断**：
  - 写入钩子配置并完成信任/重启后，必须**新开一个会话**真实触发一次回复完成，并在托肯屏幕上确认弹出提醒，才算验收通过（手动调用 `desk_show_text` 只验证设备连接，不代表钩子已生效）。
  - 可用 `desk_list_notifications` 查看电脑端是否收到提醒事件；也可在终端运行 `deskpet-platform doctor --client <客户端>`（可选 `codex`、`claude_code`、`antigravity`、`opencode`、`workbuddy`、`workbuddy_ai`；多设备加 `--device-id`，独立接收端加 `--config <接收端配置> --shared-config <原设备配置>`）核对已配对设备、钩子文件、最近触发事件与设备显示回执。`doctor` 可在 MCP 连接托肯期间同时运行。
  - 当托肯可通过 Wi-Fi 连接时，即使关闭了 MCP 会话，提醒钩子也会通过本机后台进程自动把提醒投递到托肯屏幕上。

### 屏幕消息列表、待应答计数与多设备同步

- **待应答角标与来源显示**：
  - 支持 `attention_summary` 的固件会在消息入口显示当前仍需用户拍板或回答的请求数量；支持提醒来源显示的固件还会标出来源客户端名称（多个客户端同时等待时显示「多来源」，超过 99 条显示 `99+`）。
  - 注意：「在屏幕上查看过消息」不等于「已在 Agent 中回复」。当电脑端收到用户的实际答复、取消操作或新一轮对话更新后，待应答数量会自动同步扣减；电脑断线或数据过期后角标自动隐藏。
- **查看与管理屏幕消息**：
  - 托肯首页合并展示「完成提醒」与「待应答提醒」。轻触屏幕通知区，或调用 `desk_message_view(mode="list")` 可打开屏幕消息列表。
  - 固件在后台将消息列表与详情写入 TF 卡，点开时直接读卡，因此断开电脑时仍可翻阅卡上消息，重连后自动同步已读回执与答复状态。
  - 待应答消息在列表中优先置顶（每个 Agent 会话显示最新一条）。进入列表后，当时已有的消息会统一标为已读：已读的「完成提醒」会退出屏幕提醒列表（仍可在电脑端通过 `desk_list_notifications` 查看），而已读的「待应答提醒」会保留到你在 Agent 中完成答复、取消或在屏幕上手动清除为止。
  - **多台托肯同步撤下**：当你已在 Agent 里回答了问题，或在支持清除待应答的托肯屏幕上确认清除后，共用同一套配置的托肯都会同步撤下该条待应答提醒（离线设备在重连后同步撤下；在屏幕上手动清除仅隐藏屏幕提醒，不影响你在电脑 Agent 中继续回答）。
  - **电脑端提醒管理**：`desk_list_notifications` 和 `desk_mark_notification_seen` 仅管理托肯自己的提醒记录，不会修改 Agent 客户端本身的未读状态。（旧版/参考固件在内存中缓存最近 10 条提醒，支持用 `desk_message_view` 的 `open`、`previous`、`next`、`mark_seen` 远程翻页、查看详情和标记已读）。

---

## 实时测速、Agent 额度与电脑状态监控

### 1. Token 速度采集（`AgentSpeedProbe`）

- **自动识别随包采集器**：托肯首页右栏实时显示 Token 输出速率（`t/s`）、时间、活跃会话数与额度，底部折线图显示近期速度趋势。各系统桌面包已内置预编译的 `AgentSpeedProbe`，安装后默认自动启用，多台托肯共用同一个采集进程。
- **首页总速度**：Agent Speed 1.8.0 将 Devin CLI、Qoder IDE 和 Qoder CLI 的本地 Token 速度计入首页总值。Devin Desktop 使用本地 agent 时也会计入。Qoder CLI 自带模型需要开启用量记录（国服 `QODERCN_EXPOSE_TOKEN_USAGE=1`，国际版 `QODER_EXPOSE_TOKEN_USAGE=1`），自定义模型有用量记录时可直接采集。
- **自定义或关闭采集器**：
  - 也可在启动 MCP 或后台服务时通过 `--token-probe <程序绝对路径>` 指定采集器，或将采集器放入设备配置目录的 `bin/` 下（优先于随包版本：Windows 依次查找 `bin/AgentSpeedProbe.exe` 和 `bin/AgentSpeed.Probe.exe`，macOS 和 Linux 查找 `bin/AgentSpeedProbe`）。
  - 启动时传入 `mcp --no-token-probe` 可关闭测速（配对、屏幕控制与消息提醒照常工作）。
  - 若需兼容旧版多来源采集目录，可在启动时传入 `--token-collector <目录绝对路径>`（不能与 `--token-probe` 同时指定）；`--token-sessions` 可单独指定用作额度回退的 Codex 会话目录。
- **测速查询与协议上限**：
  - 调用 `desk_token_status` 可读取最近一次发送到托肯的 Token 速率、趋势、会话数与额度状态。
  - 控制端断开或连续 15 秒无数据送达时，托肯会自动清除旧速率并将角色切回待机。
  - 当前通信协议上限为 `9999.9 t/s` 和 `255` 个会话；超出范围时暂停更新或显示未知，绝不会截断成上限值冒充真实数据。
- **自动档位与自适应比较窗口**：
  - 调用 `desk_set_role_auto(enabled=true/false)` 可开启或关闭角色随 Token 速率自动换档。
  - 在托肯设置中打开 **Speed windows**（或调用 `desk_speed_windows`）可查看或同时修改自适应档位的近期窗口（`recent_seconds`：可选 120、300、600、1800、3600 秒，即 2/5/10/30/60 分钟）和长期窗口（`long_days`：可选 1、3、7 天）。设置保存在托肯上，重连后电脑按新窗口计算自动档位。

### 2. 额度来源配置（Codex / Claude Code / Antigravity）

在托肯屏幕的「监控设置」中（或通过 `desk_dashboard_config`）可选择要显示的额度来源：`Codex`、`Claude Code`、`Antigravity Gemini` 或 `Antigravity Claude/GPT`：

- **Codex**：电脑端 MCP 自动读取本机 Codex 账户接口（接口无有效额度时回退读取本机 Codex 会话记录）。账号提供积分、重置卡和周额度重置时间时会在屏幕展示（周额度以「还剩 N 小时」显示，不满 1 小时按 1 小时计；时间未同步、数据不可用或已过期时不会编造倒计时）。
- **Claude Code 与 Antigravity**：
  - 调用 `desk_set_agent_quota(source="claude_code", enabled=true)` 或 `desk_set_agent_quota(source="antigravity", enabled=true)` 启用采集（传 `enabled=false` 可移除采集并恢复原设置），随后打开对应 Agent 会话即可。
  - **Claude Code**：通过状态栏脚本采集，会保留你原有的自定义状态栏命令（Windows 上若检测到已有自定义状态栏，为避免命令环境冲突暂不自动修改；启用状态栏可能会隐藏 Claude Code 默认的部分快捷键提示）。
  - **Antigravity**：电脑端通过已登录的官方 CLI 定期轮询额度（macOS 已支持，Windows 采集与桌面应用额度接口尚待验证；支持多来源同屏的固件会同时更新各来源，单来源固件在选中 Antigravity 时刷新，停用采集即停止轮询）。
  - 当 Agent 尚未产生有效数据、会话未开始、状态栏被覆盖或数据过期时，屏幕显示 `--`（表示暂无数据，不代表额度为零）。

### 3. 监控面板轮播与电脑 CPU / 内存显示

- **监控面板配置（`desk_dashboard_config`）**：
  - 支持 `dashboard_config` 的固件可调用 `desk_dashboard_config` 查看或分别设置当前显示的数据项（`metric`：`speed` 速度、`quota` 额度、`computer` 电脑状态、`auto` 自动轮播、`weather` 天气）与额度来源（`quota_source`）。
  - 支持轮播选择的固件可通过 `rotation` 勾选参与自动轮播的一项或多项（如 `['quota', 'weather']`），并用 `interval_seconds` 设置轮播间隔（可选 5、10、20、30 或 60 秒）。自动轮播仅切换已勾选且当前有数据的项目；无可用数据时显示等待状态。设置保存在设备中，重启后继续生效。
- **电脑 CPU / 内存与呼吸灯（`desk_computer_metrics`）**：
  - 在托肯屏幕 **桌面设置 → 显示选项** 中，可分别开关呼吸灯、电脑 CPU 使用率和内存使用率，并用呼吸灯旁的 **−**、**+** 调节完整呼吸周期（1～5 秒，步进 0.5 秒，默认 1.5 秒）。
  - 电脑端也可调用 `desk_computer_metrics(cpu=true, memory=true)` 查看或修改开关（默认关闭；首次开启 CPU 需等待第二次采样，采样暂缺时显示 `--`）。离线修改的开关会在重连后自动双向同步。

### 4. 后台常驻服务（关闭 Agent 窗口后持续更新）

- 如果希望在关闭所有 Agent 窗口期间，桌上的托肯依然持续刷新 Token 速度、CPU / 内存、额度、天气与提醒，可在终端运行：
  ```sh
  deskpet-platform service
  ```
  （跳过测速可加 `--no-token-probe`，也可传 `--usb-port`）。
- **自动无感交接**：当你打开任意 Agent 的 MCP 会话需要控制托肯时，`service` 会自动让出连接；当你关闭 Agent 会话后，`service` 会自动恢复接管。
- **独立提醒接收端常驻服务**：独立接收端可运行 `deskpet-platform --config <新设备配置> receiver-service --shared-config <原设备配置> --token-probe <AgentSpeedProbe>`。
- 注意：手动启动的终端进程不会在电脑重启后自动恢复；如需开机自启，请配置系统后台服务并确保其具备局域网访问权限（macOS 候选 `launchd` 方案见仓库 `tools/macos/README.md`）。

---

## 角色、外观主题、壁纸与相册

### 1. 角色管理与动作播放

- **制作与安装指南**：想要体验内置的 `Pocket Bot`、安装现成 `.zip` 角色包或制作自定义角色，请调用 `desk_read_manual(topic="roles")` 阅读[自定义角色指南](roles.md)。
- **离线检查与安装**：
  - `desk_list_role_sources(directory=...)`：扫描本机目录中的可安装角色包。
  - `desk_check_role(source=..., preview_dir=...)`：离线检查角色目录或 `role1` ZIP 的资源完整性、命名、首页活动范围与多帧稳定性，传入 `preview_dir` 可输出 320 × 240 首页合成预览动图与互动总览。
  - `desk_install_role(source=...)`：执行同一套严格校验，通过后加密传输到托肯储存卡并启用（安装和切换角色不会打断当前屏幕页面；需切到角色主页时调用 `desk_set_view(page="role")`）。
- **查询、切换、试播与动作控制**：
  - `desk_list_roles` / `desk_select_role(identity=...)`：列出卡上已安装角色或切换到指定角色。
  - `desk_role_preview`：在图鉴中试播已安装角色（`mode="start"` 开始试播，依次演示阶段 0 待机、1～5 五档速度、6 爆气；`status` 查看当前阶段；`use` 将正在试播的角色直接设为当前角色；`cancel` 或播完后自动返回图鉴并恢复原角色）。
  - `desk_list_actions` / `desk_play_action(action=..., duration_ms=...)`：查询当前角色支持的动作并临时播放；启动回执包含 `operation` 与 `boot`，可用 `desk_action_status` 查询最近 8 次动作的播放、完成或中断状态。
  - `desk_set_role_base(action=...)`：设置持续播放的基础动作（临时动作结束后会回到该持续动作；切换角色或重启后恢复默认）。
  - **五档滑行（`glide_1` ～ `glide_5`）**：包含 DNC1 v3 滑行片段的角色包在自动模式下会按包内计时与安全帧在「跑动」与「滑行」间自然轮换，`desk_status` 的 `role.base_action` 会显示当前持续动作，也可通过 `desk_play_action` 单独调用。
  - **读卡容错**：播放速度或爆气帧若遇储存卡读取失败，托肯会自动重开文件重试一次；若仍校验失败则中断该动作并回退到可用的持续动作或待机，重新选择该角色即可重试。

### 2. 外观主题与独立壁纸

- **查看与切换主题 / 壁纸（`desk_appearance_config`）**：
  - 支持 `appearance_config` 的固件可调用 `desk_appearance_config` 查看或分别设置主题（`theme`：内置 `paper`、`night`、`sage` 或已导入的主题 JSON 文件名）和壁纸（`wallpaper`：`desk_add_photo` 返回的照片标识，或卡上的 `photos/`、`media/`、`wallpapers/` 路径；传空字符串 `""` 清除壁纸）。
  - 设置保存在设备中。屏幕休眠或播放媒体期间会拒绝修改；切换壁纸时设备需处于工作台首页或外观设置页。
- **导入自定义配色主题（`desk_add_theme`）**：
  - 支持 `theme_json_upload` 的固件可用 `desk_add_theme(source=..., name=...)` 导入配色 JSON（名称使用 1～24 位英文字母、数字、下划线或连字符）。设备会自动校验颜色字段与对比度，拒绝不合规主题；文件名按名称与内容哈希生成，重复导入不会覆盖旧文件。导入后可在设备「外观 → 更多主题」中翻页选择。
- **管理独立壁纸（`desk_add_wallpaper` / `desk_list_wallpapers` / `desk_delete_wallpaper`）**：
  - `desk_add_wallpaper(source=..., name=...)` 将图片导入 TF 卡的独立 `wallpapers/` 目录（名称规则同上），可用作背景壁纸，且**不会**混入开机相框的照片轮播。
  - `desk_list_wallpapers(cursor=...)` 分页查询所有可用壁纸、可作壁纸的相册图片及当前选中项（用返回的 `next_cursor` 查下一页）。
  - `desk_delete_wallpaper(path=...)` 删除已导入的独立壁纸（当前正在使用的壁纸需先切换后再删除）。

### 3. 离线相册与储存卡文件管理

- **导入与控制相册（`desk_add_photo` / `desk_photo_album`）**：
  - `desk_add_photo(source=...)` 将电脑图片转换后连同缩略图一起传入托肯相册（支持 `photo_jpeg_upload` 的固件会保留原图比例生成高质量 JPEG，也可在壁纸列表中选用；支持全屏相册的固件会同时生成铺满宽或高的版本，早期照片重新导入原图即可补齐；重复导入相同图片不会覆盖已有文件）。
  - `desk_photo_album` 控制屏幕相册：`grid` 打开或刷新缩略图网格，`open` 传入照片标识打开大图，`previous` / `next` 在网格中翻页或在大图中切图，`play` / `pause` 控制自动轮播，`interval` 设置轮播间隔（5、10 或 15 秒）。在托肯全图页也可轻触左右箭头切图、轻触中央隐藏/显示按钮、点右上角进入轮播设置。
- **底层文件上传与资源删除（`desk_upload_files` / `desk_delete_resource`）**：
  - `desk_upload_files` 仅用于向储存卡 `desk/` 或 `photos/` 传输底层受支持文件（声明 `card_restricted_paths` 的固件只接受合规的专用导入入口，不接受任意文件；安装角色必须用 `desk_install_role`，导入相册图片请用 `desk_add_photo`）。
  - 支持 `card_delete` 的固件可用 `desk_delete_resource(path=...)` 删除 `roles/<角色标识>` 整套角色、`photos/<照片标识>` 照片及缩略图，或 `desk/` 下的单个文件（删除当前正在使用或试播前保存的角色前，请先切换到其他角色）。

---

## 音乐播放与曲库

支持曲库管理的设备可直接从电脑导入和删除曲目，储存卡可以留在设备里。

- `desk_music_library(action="import", source=...)` 接收电脑上 MP3 或 FLAC 文件的绝对路径。默认放在 `music/`，也可用 `path` 指定 `music/曲名.flac` 或 `media/曲名.mp3`。导入返回 `completed` 后可用 `desk_music_library(action="list")` 查看当前播放顺序。
- `desk_music_control(action="play", path=...)` 播放指定歌曲，`path` 使用曲库返回的完整路径。也可用 `status` 查询，或用 `pause`、`stop`、`prev`、`next` 控制。返回工作台后音乐继续播放，播到列表末尾自动停止。
- 在屏上点「打乱顺序」，或调用 `desk_music_control(action="shuffle")`，会生成并保存一份新顺序。重启后仍沿用这份顺序，直到再次主动打乱。曲目太少、无法生成不同顺序时会有提示。
- 在屏上点歌曲旁的「删除」并确认，或显式调用 `desk_music_library(action="delete", path=...)`。`music/` 和 `media/` 中的同名曲目路径不同，请从列表选择要删的一条。遇到 `music_ack_lost` 时，重新列出曲库核对该路径是否还在。

---

## 页面切换、时钟、天气与屏幕控制

- **页面切换与默认首页（`desk_set_view`）**：
  - 调用 `desk_set_view(page=...)` 可打开角色工作台（`role`）、应用菜单（`home`）、时钟（`clock`）、角色图鉴（`roles`）、相册（`photos`）、天气（`weather`）、消息列表（`messages`）或设置（`settings`）。
  - 传入 `make_default=true` 可将 `role`、`home`、`clock` 或 `photos` 设为开机默认首页（将相册设为默认首页时，开机自动轮播相册照片）。
- **时钟表盘与校时（`desk_sync_clock` / `desk_clock_edit`）**：
  - 时钟页提供 6 种表盘（可通过 `desk_set_view(page="clock", clock_face=0..5)` 选择）。托肯开机联网后会自动校时，MCP 连接时也会自动补齐尚未设置的时间或时区。
  - 调用 `desk_sync_clock` 可主动用电脑时间校准托肯（可指定 `offset_minutes` 时区偏移）。
  - 在托肯屏幕进入 **桌面设置 → 校时**（旧版/参考固件为时钟页上的 **Set time**），可在 **日期时间** 页签用 **−**、**+** 调整年/月/日/时/分并点 **保存**，在 **时区** 页签按 15 分钟步进调整时区偏移。电脑端也可先打开时钟页，再用 `desk_clock_edit`（`start` → `adjust` → `save` / `cancel`）远程操作同一编辑器。
- **天气与七天预报**：
  - 托肯联网后通过自身 Wi-Fi 自动定位，在首页右上角显示城市与当前天气，轻触该区域可查看七天预报（断网时预报页会标示缓存；旧版/参考固件可调用 `desk_set_weather_location` 手动设定经纬度与英文地名）。
  - 天气与七天预报数据由 [Open-Meteo](https://open-meteo.com/) 提供，采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 许可；默认使用的[免费接口](https://open-meteo.com/en/terms)仅供非商业用途。
- **短文字、临时图片、屏幕亮度与触摸校准**：
  - `desk_show_text(text=...)`：在屏幕显示两行以内的短文字（支持中英文），返回显示回执。
  - `desk_show_image(source=...)`：将电脑图片自动转换后传到储存卡并全屏展示；轻触屏幕或调用 `desk_clear_image` 返回原页面。
  - `desk_set_brightness(level=0..100)`：设置并保存屏幕亮度；设为 `0` 会关闭背光，轻触屏幕即可重新点亮。
  - `desk_calibrate_touch`：在屏幕上启动三点触摸校准（按提示依次点击三个十字标记；长按 3 秒或调用 `desk_cancel_touch_calibration` 可取消并保留原校准）。完成校准后，`desk_wait_touch` 可读取点击位置，轻触屏幕上的角色区域也会本地触发角色的 `touched` 互动动作。

### ES3C35P 的电池读数与屏幕休眠

- 支持 `battery_status` 的固件在「设置 → 显示」提供电池读数，电脑端调用 `desk_status` 可查看 `battery`。现阶段提供电池电压，供电来源显示「状态未知」，电量百分比留空。刚开机或采样失效时，电压也可能暂时留空。
- 有「休眠」入口的固件可在「设置 → 系统」立即休眠，在「设置 → 显示」选择自动休眠时间及微亮时钟或纯黑息屏。轻触屏幕后会出现确认，点「退出休眠」恢复亮度，点「继续休眠」或等待约六秒会回到休眠。

---

## 语音朗读与语音交互（实验功能）

### 1. 文字与提醒朗读

- **主动朗读**：在支持语音的设备上，`desk_speak(text=...)` 可朗读不超过 80 字的单行文字；`desk_read_notification(identity=...)` 可按编号朗读指定提醒的标题（不会改变提醒的已读状态）。
- **屏幕点按朗读**：支持 `message_read_aloud` 的固件可在提醒详情页点击「联网朗读」，由电脑端合成并播放该条标题（电脑断线时不会自动重播）。
- **隐私与平台说明**：文字仅在主动触发朗读时发送至微软 Edge 在线语音服务，由电脑分段临时合成音频（每段最长 5 秒），设备仅在内存中接收播放，不会写入储存卡。电脑端音频转换当前需要 macOS 环境；设备正在播放音乐或视频时会拒绝朗读。

### 2. 语音识别、唤醒对话与工具授权

语音识别与对话功能目前处于开发验证阶段。普通「开始录音」会将录音传给电脑识别并在屏幕显示结果；「语音唤醒」还会将识别出的文字发给对话模型，并将模型回复合成为语音在托肯上播放。

- **配置语音识别服务（在电脑端进程环境变量中三选一，同时开启多种会停用识别）**：
  1. **阿里云百炼（华北 2 · 北京）**：设置 `DESKPET_ENABLE_DASHSCOPE_TRANSCRIPTION=1` 和 `DASHSCOPE_API_KEY`（可选 `DASHSCOPE_WORKSPACE_ID`），电脑通过 WebSocket 调用 `paraformer-realtime-v2` 实时识别。
     macOS 和 Linux 的后台进程可用 `DASHSCOPE_API_KEY_FILE` 指定单行密钥文件的绝对路径。文件须属于接收器账号、权限为 `0600` 或 `0400`，且不能是符号链接。直接设置的 `DASHSCOPE_API_KEY` 优先。
  2. **硅基流动**：设置 `DESKPET_ENABLE_ONLINE_TRANSCRIPTION=1`、`DESKPET_ASR_API_KEY` 和 `DESKPET_ASR_MODEL`。
  3. **本地 whisper.cpp 识别**：设置 `DESKPET_ENABLE_LOCAL_TRANSCRIPTION=1`，并用绝对路径配置 `DESKPET_LOCAL_ASR_CLI`、`DESKPET_LOCAL_ASR_MODEL` 和 `DESKPET_LOCAL_ASR_VAD_MODEL`（需支持 stdin 与 VAD），录音在电脑本机通过内存管道完成识别。
- **配置语音唤醒对话模型**：在电脑端额外设置 `LLM_BETA_OPENAI_BASE_URL`、`LLM_BETA_OPENAI_API_KEY` 和 `LLM_BETA_OPENAI_MODEL`。
  - 对话服务支持 JSON 模式时，可设置 `LLM_BETA_OPENAI_TOOL_FORMAT=json`。模型判断用户要执行的操作，电脑按当前设备授权决定是否调用。服务支持推理开关时，可用 `LLM_BETA_OPENAI_ENABLE_THINKING=false` 缩短对话等待。
- **昵称与语音工具授权（`desk_voice_names` / `desk_voice_tools`）**：
  - `desk_voice_names` 可查看或修改保存在托肯上的用户昵称与设备昵称（用户昵称可为空；修改昵称不会改变唤醒词）。
  - `desk_voice_tools` 查看或修改某台已配对托肯的语音工具授权（默认全部关闭；传入完整的 `enabled` 列表替换当前授权，传空列表 `[]` 撤销全部）。可授权项包括：查看设备状态 `device_status`、查看角色动作 `list_role_actions`、显示工作台并播放角色动作 `play_role_action`。授权后语音助手可按用户语音指令直接调用，无需逐次确认；变更授权后会在下次对话时重建工具列表并清空旧对话上下文。
  - 授权列表的 `device_supported` 显示当前设备是否支持该项。调用前会复查授权。动作回执报告已开始或未开始，失败时会提示未授权、设备忙碌或连接等原因。

---

## 网络切换、诊断、重启与 USB 备用连接

- **切换已保存的 Wi-Fi（`desk_select_network`）**：
  - 调用 `desk_select_network(ssid=...)` 可按网络名称（有效 UTF-8）切换到托肯已保存的其他 Wi-Fi（旧版固件不支持此命令时请直接在屏幕 Wi-Fi 设置中切换）。
  - 切换过程会短暂断开无线连接；重连后可调用 `desk_diagnostics` 核对 `network_switch` 的操作编号与结果。切换成功后目标网络会成为重启后的首选网络；若目标网络无法连接，托肯会自动尝试恢复原网络。
- **储存卡状态与重挂载（`desk_diagnostics`）**：
  - `desk_diagnostics` 可查看当前连接、已保存网络、重启原因及储存卡空间，`card.mount_count` 记录本次开机以来储存卡成功挂载的次数。
  - 若储存卡接触恢复后需重新挂载，在屏幕上重新打开「角色图鉴」或「相册」即可触发介质探测与挂载，随后返回角色页会重新加载上次保存的角色。
- **子系统恢复与软重启**：
  - 有对应功能的设备，可在「设置 → 系统」分别恢复储存卡、触摸、声音或 Wi-Fi，也可使用 `desk_recover_subsystem(target=...)`，四项分别对应 `sd`、`i2c`、`audio`、`wifi`。`accepted` 表示设备已受理，执行结果见 `desk_diagnostics` 的 `diagnostics.last_subsystem`。
  - 屏上「重启」需要点按确认。`desk_restart` 会直接请求已配对的设备软重启。收到 `accepted` 后等设备重新连接，再用 `desk_status` 或 `desk_diagnostics` 检查。收到 `restart_ack_lost` 时也先检查状态，避免重复请求。若重启时仍有写入待确认，`diagnostics.restart_pending_writes` 会列出对应类别，便于核对曲库、消息缓存或木鱼计数。
- **原生 USB 应急复位（`desk_usb_reset`）**：ES3C35P 的无线和屏上入口都无法响应时，可用数据线连接设备。调用时传入已配对设备的持久 UUID 和已核对的原生 ESP 串口。工具会核对 USB 序列号和配对 MAC，复位后可用 `desk_status` 检查连接。
- **USB 身份查询与无线断连备用串口（`--usb-port` / `desk_set_usb_fallback`）**：
  - 在终端运行 `deskpet-platform find-usb --device-id <设备UUID> /dev/cu.usbserial-1140`（或在 MCP 中调用 `desk_find_usb_device`），可在不抢占现有 Wi-Fi 控制连接的前提下，读取指定串口上的设备 MAC 与持久 UUID（省略 `--device-id` 可列出所有候选串口的匹配结果）。
  - 启动 MCP 时可通过 `--usb-port <串口>` 指定备用串口（启动时未插 USB 也能正常用 Wi-Fi 启动）；运行期间若串口路径改变或需为第二台设备设置备用串口，可先用 `desk_find_usb_device` 查出串口，再调用 `desk_set_usb_fallback(port=..., device_id=...)`（需在无线连接正常时更新）。
  - 配置备用串口后，一旦 Wi-Fi 连接中断或握手超时，电脑会核对串口设备的 MAC 与 UUID 并自动切到 USB 继续控制和写卡；待无线连通恢复且当前传输结束后，MCP 会自动切回 Wi-Fi 并保存新的局域网地址。当前使用的传输方式（`wifi` 或 `usb`）可通过 `desk_status` 的 `transport` 字段查看。
