# DeskPet 操作手册

## 首次连接

1. 在电脑本机安装公开底座包，准备 Python 3.11 或更新版本。在 Agent 中配置 stdio MCP，命令为安装得到的 `deskpet-platform` 的绝对路径，参数为 `mcp`。[Python 下载](https://www.python.org/downloads/macos/)
2. 用 USB 将小板连接到电脑。调用 `desk_pair_usb`，把已确认的串口填入 `port`，把登记的设备 MAC 填入 `expected_identity`。需要同时配网时，在 `credentials_file` 中提供本机 Wi‑Fi 凭据文件的绝对路径。文件权限为 `0600`，JSON 包含 `ssid` 和 `password` 两个字符串，开放网络的密码为空。
3. 用 `desk_status` 检查连接，再调用 `desk_show_text`。返回 `shown` 或 `finished` 表示设备报告实际绘制。小板已联网但电脑暂时无法访问时，可以通过 USB 完成配对。若 USB 配网失败，小板会尝试恢复原有网络。

### Codex 的写卡等待时间

用 Codex 通过 Wi-Fi 传输单个 8 MiB 文件时，在 Codex 的 `config.toml` 中为 DeskPet 的 MCP 条目设置 1,200 秒。已登记时，在现有表中补这一项。

```toml
[mcp_servers.deskpet]
command = "/安装路径/deskpet-platform"
args = ["mcp"]
tool_timeout_sec = 1200
```

大文件和批量安装需要按文件大小和数量延长客户端等待时间。设置后从新会话连接。

### 手机扫码配网和配对

1. 给小板供电。完整显示固件从应用菜单进入 **桌面设置 → Wi-Fi**。参考固件在屏幕未显示配网二维码时，长按屏幕 3 秒打开 Wi-Fi 设置。用手机扫描左侧二维码加入小板热点，再扫描右侧二维码打开设置页。选择 2.4 GHz Wi-Fi 并输入密码，列表中找不到网络时可手动输入名称。联网后，让手机和电脑回到与小板相同的网络。
2. 调用 `desk_pair_begin` 打开电脑本机配对页。完整显示固件从应用菜单进入 **桌面设置 → 电脑配对**。参考固件尚未校准触屏时，轻触已联网的小板显示配对码。用手机扫描配对码，将内容粘贴到本机页面。自动发现失败时，填写小板显示的局域网地址。也可在本机终端运行 `deskpet-platform pair`，将码粘贴到隐藏输入框。
3. 用 `desk_pair_status` 查看配对页结果，再用 `desk_status` 检查连接。

连接显示 `busy` 时，停止另一个控制同一设备的服务或 MCP 实例。连接显示 `offline` 时，先检查设备供电、Wi‑Fi 和配对。运行 MCP 时用工具查状态，终端的独立 `status` 也要临时占用连接。

### 多台设备

- 添加另一台时，在 `desk_pair_usb` 或 `desk_pair_begin` 中设置 `add_device=true`；原有设备的配对不会被替换。USB 操作仍需先确认串口对应的设备。
- 用 `desk_list_devices` 查看本机已配对设备。新固件提供设备自身保存的 UUID；旧固件暂时显示以 `mac:` 开头的身份，更新固件并成功连接后可升级为设备 UUID。
- 给已配对的小板通过 USB 配置 Wi‑Fi 时，在 `desk_pair_usb` 中传入这台小板的 `device_id`、已确认的串口和 MAC。
- 只有一台设备时，原有操作可以不指定设备。存在多台时，在查询和控制工具中填写 `device_id`，值取自列表中的 `id`；不填写会提示选择。提醒的启用和关闭也使用这个参数，分别管理每台设备。
- 在本机终端添加另一台时使用 `deskpet-platform pair --add-device`；独立查询使用 `deskpet-platform status --device-id <列表中的 id>`，并先释放同一设备的 MCP 连接。

## 日常操作

- `desk_status` 在连接时向小板读取最新状态。储存卡操作期间返回最近一次读数。
- `desk_show_text` 显示两行以内的短文字，支持电脑字体能够绘制的中文；返回显示回执。
- `desk_show_image` 接收电脑图片的绝对路径，自动转换后传到储存卡并显示。轻触屏幕或调用 `desk_clear_image` 可返回。
- `desk_set_brightness` 设置 0–100 的屏幕亮度。设为 0 后触摸屏幕可重新点亮。
- `desk_calibrate_touch` 在屏幕启动三点校准。按屏幕提示触摸三个标记，校准后 `desk_wait_touch` 返回点击位置。长按 3 秒或调用 `desk_cancel_touch_calibration` 可取消校准。校准后，点击角色可播放该角色提供的 `touched` 动作。
- MCP 启动命令可在 `mcp` 后增加 `--role-dir <角色素材目录的绝对路径>`。素材目录由你在电脑上选择。`desk_list_role_sources` 查询这个目录里的可安装角色；也可向该工具传入其他角色素材目录的绝对路径。
- 用 `desk_find_usb_device` 读取候选串口上的小板 MAC 和持久 UUID。传入 `ports`（macOS 可用 `ls /dev/cu.usbserial-*` 查看路径），并用 `device_id` 指定要找的小板。查询无需配对，Wi-Fi 正在使用时也可读取。老固件可能没有这条查询命令，已配对设备的 UUID 仍可从 `desk_list_devices` 查看。
- MCP 启动时可用 `--usb-port` 指定已确认的备用串口，未接 USB 时仍可启动并使用 Wi-Fi。Wi-Fi 连不上或握手超时时会尝试 USB，可继续控制小板和传卡。备用串口在启动后才出现或被替换时，电脑先比对配对记录中的 MAC 和 UUID，再接入 USB。旧配对尚无 UUID 时，可先让支持 UUID 的小板经 Wi-Fi 完成一次认证或重新配对。若串口路径改变，先用 `desk_find_usb_device` 找到新路径。无线连通验证通过且当前动作和传输结束后，MCP 会自动切回 Wi-Fi，并保存新地址供下次连接。`desk_status` 可查看当前连接方式。
- 用 `desk_select_network` 切换已保存的 Wi-Fi。切换会短暂断开无线控制。重连后用 `desk_diagnostics` 查看本次操作的 `network_switch`、当前网络和卡空间。切换成功后，目标网络成为重启后的首选。目标连不上时，小屏会优先恢复原连接。
- 储存卡恢复连接后，重新打开角色图鉴或相册以探测介质并在需要时重新挂载。返回角色页会重新加载上次保存的角色。`desk_diagnostics` 的 `card.mount_count` 是本次开机以来成功挂载的次数。
- `desk_set_view` 可打开角色、应用菜单、时钟、角色图鉴、相册、天气、消息或设置。可把角色、应用菜单、时钟或相册设为默认首页。相册作为默认首页时，开机自动轮播已有照片。
- `desk_role_preview` 可以试播已安装的角色。用 `start` 并传入角色标识开始，用 `status` 查进度，`cancel` 返回图鉴，`use` 启用正在试播的角色。阶段 0 为待机，1 至 5 为对应档位，6 为爆气。试播播完会自动返回图鉴并恢复原角色。
- 时钟提供六种表盘。小屏开机联网后会自动校时。MCP 接入时补齐尚未设置的时间或时区，保留已有值。用 `desk_sync_clock` 可主动同步电脑的时间，也可指定时区偏移。
- 在小屏打开 **桌面设置 → 校时**。在 **日期时间** 页签选择年、月、日、时或分，用 **−**、**+** 调整，再点 **保存**。切到 **时区** 页签可按 15 分钟调整偏移，保存后重启仍有效。点 **取消** 可放弃编辑。参考固件的校时入口是时钟页上的 **Set time**。
- `desk_clock_edit` 可查看小屏时间并使用同一编辑器。先调用 `desk_set_view(page="clock")` 打开时钟页，再用 `start` 开始，`adjust` 逐项修改，`save` 应用或 `cancel` 放弃。
- `desk_add_photo` 将本机图片和缩略图一起传到相册。用 `desk_photo_album` 的 `grid` 打开或刷新缩略图，再用 `open` 和返回的照片标识打开照片。`previous` 和 `next` 在网格中翻页，在照片中切图。`play` 和 `pause` 控制轮播，`interval` 保存 5、10 或 15 秒的间隔。完整显示固件的全图页可点左右箭头切图，点中央隐藏或显示按钮，右上角进入轮播设置。支持全屏相册的固件会为照片保留宽或高铺满屏幕的版本，重新导入原图即可为早期照片补齐这个版本。
- 完整显示固件通过小屏自己的 Wi-Fi 定位，首页右上角显示城市和天气，点按该区域可查看七天预报。断网时预报页会标示缓存。参考固件可用 `desk_set_weather_location` 为选定设备设置地点，保存后由小屏自行刷新。
- 本机 MCP 自动读取 Codex 的数字用量。小屏右栏显示 Token 速率、时间、会话数和额度，底部显示近期趋势。用 `desk_set_role_auto` 开关自动档位。控制端断开或 15 秒没有收到数据时，小屏清除旧速率，自动档位回到待机。
- Token 速度使用原版 `AgentSpeedProbe`：将可执行文件放在设备配置目录的 `bin/AgentSpeedProbe`，或在启动 MCP 时传入 `--token-probe <程序绝对路径>`。多台设备共用一个采集进程。原项目本身包含实测和按近期速度估算的数据；小屏显示速度与 `t/s`，`desk_token_status` 保留数据状态供查询。使用 `mcp --no-token-probe` 可跳过测速，设备配置目录中已有采集程序时也适用。未配置采集程序时，速度显示不可用。`--token-sessions` 可指定用作额度回退的 Codex 会话目录，不改变测速来源。
- 当前小屏协议最多传输 9999.9 t/s 和 255 个会话。速度超出范围时暂停更新，会话数超出范围时显示未知；不会把它们截成上限后冒充真实值。完整范围需要后续更新小屏协议。
- 账号提供积分、重置卡和周额度重置时间时，支持的固件会显示这些数据。周额度以“还剩 N 小时”显示，未满一小时按一小时显示；时间未同步、数据不可用或已过期时不编造倒计时。
- 在小屏 **桌面设置 → 显示选项** 中可分别开关呼吸灯、CPU 使用率和内存使用率，选择保存在该设备。CPU 和内存默认关闭，也可用 `desk_computer_metrics` 设置，例如 `cpu=true, memory=true`。省略两个开关可查看当前设置。电脑离线修改的开关会在重连后同步，同期在小屏修改的另一项也会保留。首次开启 CPU 时需等待第二次采样。采样暂缺时显示 `--`。
- 在设置中打开 **Speed windows**，点按近期或长期一行切换选项，再点 **Save**。也可用 `desk_speed_windows` 查看或同时设置 `recent_seconds` 和 `long_days`。近期可选 2、5、10、30、60 分钟，长期可选 1、3、7 天。选择保存在小屏，重连后电脑按新窗口重新计算自动档位。
- 要复用原桌面屏项目的多来源 Token 采集器，启动 MCP 时传入 `--token-collector <原项目绝对路径>`。采集结果只包含数字汇总，采集器按原项目支持的来源查找本机记录。账号接口没有有效额度时，MCP 会从本机 Codex 会话记录读取额度。
- 移植候选固件在主页合并完成提醒，并显示最多三行待应答。点通知区进入带 Agent 标识和数字页码的列表，打开详情标为已读。收到来源 Agent 的答复或失败事件后，才清除对应的待应答。支持离线详情的固件会把最近 100 条已同步消息保存到储存卡。断开电脑后，可从角色页底部打开列表，点开某条详情后，该条已读状态会在重连时同步。
- 参考固件的小屏消息页缓存最近十条提醒。`desk_message_view` 可打开参考固件的小屏列表、翻页和查看详情。`open` 和 `mark_seen` 需要通知编号，标记时须显示该条详情。也可在屏上点详情或弹窗的已读按钮。离线标记在重连后同步回电脑上的小屏提醒记录。
- 想用一张图或一句话做自己的角色，或先装一个示例，可以用 `desk_read_manual(topic="roles")` 打开自定义角色指南。
- `desk_check_role` 接收电脑上的角色目录或 role1 ZIP 绝对路径，离线检查资源、命名和格式。`desk_install_role` 使用同一套检查，通过后才传输到储存卡，由小板校验并启用。完整显示固件的图鉴使用 role1 ZIP。`desk_list_roles`、`desk_select_role`、`desk_list_actions` 和 `desk_play_action` 用于查询、切换和播放。安装和切换角色会保留当前页面。需要打开角色页时，用 `desk_set_view` 选择 `role`。
- 部分角色包还提供五档滑行。自动模式按包内的计时和安全帧在跑动与滑行之间切换。`desk_status` 的 `role.base_action` 显示当前持续动作，也可用 `desk_play_action` 单独播放 `glide_1` 至 `glide_5`。
- `desk_play_action` 的启动回执包含 `operation` 和 `boot`，可用 `desk_action_status` 查询最近八次动作的播放、完成或中断状态。重启后使用新的回执。
- role1 的速度或爆气帧读卡失败时，小板会重新打开文件读取一次。如果仍校验失败，该动作会中断，小板会尝试继续播放可用的持续动作或待机。重新选择角色可重试出错动作。
- `desk_set_role_base` 设置持续播放的角色动作；临时动作结束后回到持续动作。切换角色或重启后恢复该角色默认动作。
- `desk_upload_files` 可以传输 `desk/` 和 `photos/` 下的文件。写入 `roles/` 使用 `desk_install_role`。
- `desk_delete_resource` 可删除 `roles/<角色标识>` 对应的完整角色、`photos/<照片标识>` 对应的照片及缩略图，或 `desk/` 下的单个文件。删除当前角色或试播前保存的角色前，先切换到其他角色。
- 每种 Agent 在电脑上启用一次，提醒会发给这套配置中所有已配对的小屏。离线设备重新连接后会补收。每台小屏各自保存投递与已读状态。在 Agent 里答复后，各设备都清除该条待应答提示。调亮度、切换角色和查看单台设备的提醒仍按设备 ID 选择。
- 用 `desk_enable_codex_notifications` 启用 Codex 提醒，再到 Codex 的 `/hooks` 或设置中的 Hooks 审核并信任新增条目。工具会返回写入的 `hooks.json` 路径。为 MCP 设置 `CODEX_HOME` 后，启用和停用操作都使用该目录。小屏可通过 Wi-Fi 连接时，关闭 MCP 后也可由本机后台进程投递。`desk_list_notifications` 和 `desk_mark_notification_seen` 管理小屏自己的提醒记录。`desk_disable_codex_notifications` 移除本程序的 hook，保留已有提醒。[Codex Hooks 说明](https://developers.openai.com/codex/hooks)
- 用 `desk_set_claude_notifications` 启用或关闭 Claude Code 提醒。完成提醒已通过真实 Claude Code 2.1.32 的隔离会话验证。结构化提问及处理结果已通过样本测试。[Claude Code Hooks 说明](https://code.claude.com/docs/en/hooks)
- 用 `desk_set_antigravity_notifications` 启用或关闭 Antigravity 的完成和提问提醒。已启用旧版提醒的用户可再次启用以更新钩子。Antigravity CLI 1.5.7 已通过真实完成和提问钩子测试。桌面应用和授权弹窗仍待验证。[Antigravity Hooks 说明](https://antigravity.google/docs/hooks)
- 用 `desk_set_opencode_notifications` 启用或关闭 OpenCode 完成、提问和授权提醒。启用后重启 OpenCode。OpenCode 1.18.25 的隔离实例已验证完成、提问、答复和拒绝授权。实机验证拒绝授权只清除对应请求。日常桌面配置仍待验收。[OpenCode 插件配置](https://opencode.ai/docs/plugins/)
- 用 `desk_set_workbuddy_notifications` 启用或关闭 WorkBuddy 的完成、提问和授权提醒。WorkBuddy 使用默认的 `workbuddy`，WorkBuddy AI 使用 `workbuddy_ai`。启用后重启相应应用。提问按请求编号清除。授权按会话轮次合并，并在该轮结束时清除。两版内置 CLI 已通过完成提醒测试，WorkBuddy 5.1.5 另通过授权提示和拒绝后清除的实机测试。桌面应用及 WorkBuddy AI 授权仍待验收。

用 `desk_read_manual` 可以查看操作手册（`operations`）、二次开发手册（`development`）和自定义角色指南（`roles`）。更换电脑前请确认上一台已断开。删除本机凭据会停止本机自动连接，设备端撤销授权需按对应固件的流程操作。
