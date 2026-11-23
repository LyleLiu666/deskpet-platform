# ES3C35P 可选 P1 能力（协议评审稿）

此文供电脑端与固件共同评审。切页、相册控制和 P1 公共字段沿用现有协议。下文的扩展能力握手、音乐、诊断、电池和板载助手消息是拟新增或扩展的接口，验收通过后由固件在握手中逐项声明。电脑端只对能力值为 1 的设备开放对应入口。

## 消息格式与握手容量（1024 / 1152 字节边界）

请求和回包均为一行 `P1,` 加 JSON 和换行。请求携带当前连接的 `epoch`、递增的 `seq` 和命令 `type`。回包原样带回 `epoch`、`seq`，并带当前启动编号 `boot`、布尔值 `ok`。失败回包给出字符串 `error`。按最终编码计算，包含前缀和换行的请求最多 **1024 字节**，回包最多 **1152 字节**。示例使用合成数据。[电脑端编解码](../src/deskpet_platform/channel.py)

### 扩展能力握手（`cap_ext` 与 `capabilities_ext`）

ES3C35P 现有 `hello` 回包在最坏合法头部（48 字节 `epoch`、10 位 `seq`/`boot`）并裁剪 `role1_required`、`dashboard_interval`、`dashboard_rotation` 三个可选字段后已达 **1133 字节**，仅余 19 字节，无法直接塞入 Base 语音子能力与本次新增能力。为同时满足 1152 字节上限与「旧电脑端 × 新固件」「新电脑端 × 旧固件」「新电脑端 × 新固件」三向兼容：

1. 新固件在 `hello` 回包中保持现有基础能力不变，仅新增 `"cap_ext":1`（占 12 字节，最坏总长 **1145 字节 ≤ 1152 字节**）。旧电脑端忽略未知字段 `cap_ext`，已有功能不受影响。
2. 新电脑端在 `Channel.handshake()` 收到 `hello` 后，若 `response.get("cap_ext") == 1`，在同一握手锁内立即发送 `capabilities_ext`，将返回的扩展能力字典合并进 `channel.capabilities` 后再置 `online = True`。旧固件未声明 `cap_ext` 时不发送该请求。

扩展响应沿用同一连接和启动编号。扩展能力值为整数 1。基础与扩展都出现的能力可以合并，值须一致。设备身份和协议版本仅取自 `hello`。扩展响应校验失败时，电脑端结束本次握手并保持离线。

```text
P1,{"epoch":"demo","seq":1,"type":"hello"}
P1,{"epoch":"demo","seq":1,"boot":4,"type":"capabilities","mac":"ec:d6:1b:3b:25:40","protocol":1,"rx_bytes":2048,"token_stats":1,"device_info":1,"cap_ext":1,"ok":true}
P1,{"epoch":"demo","seq":2,"type":"capabilities_ext"}
P1,{"epoch":"demo","seq":2,"boot":4,"dashboard_rotation":1,"dashboard_interval":1,"screen_views":1,"photo_album":1,"battery_status":1,"subsystem_recover":1,"system_restart":1,"music_control":1,"music_library":1,"assistant_onboard":1,"capture_media_tools":1,"ok":true}
```

### 复用现有 Base 切页与相册协议

- **切页（`open_page`）：** 依赖现有 `screen_views: 1`，调用 `screen_view`。例如临时打开首页时 `page` 取现有页面表中 `home` 的编号 `1`，并传 `make_default: false`。消息详情和列表使用独立的 `message_view` 能力；仅声明 `message_view` 时，电脑端隐藏通用切页工具。[切页实现与页面表](../src/deskpet_platform/client.py)
- **相册与轮播（`photo_album`）：** 普通电脑端命令的 `mode` 支持 `status`、`grid`、`open`、`previous`、`next`、`play`、`pause`、`interval`。`open` 带列表返回的照片 `id`，`interval` 带 `seconds`，取 `5`、`10` 或 `15`。ES3C35P 从卡上当前相册来源读取 JPEG，并返回 `view`、`playing`、`interval_seconds` 和照片标识。照片列表每页最多四项，页码从 0 开始。

```text
P1,{"epoch":"demo","seq":3,"type":"screen_view","page":1,"make_default":false}
P1,{"epoch":"demo","seq":3,"boot":4,"ok":true}
P1,{"epoch":"demo","seq":4,"type":"photo_album","mode":"play"}
P1,{"epoch":"demo","seq":4,"boot":4,"view":"photo","playing":true,"interval_seconds":10,"count":2,"page":0,"pages":1,"photos":["p-0123456789abcdef","p-1123456789abcdef"],"photo":"p-0123456789abcdef","photo_drawn":true,"photo_draws":1,"ok":true}
```

## 新增与扩展命令汇总

以下表格仅列命令和业务字段。公共字段与错误格式均按上例处理。设备未知命令沿用 `unsupported`，字段不合法返回 `invalid_fields`，资源被占用返回 `busy`。状态查询可在重连后重读。切歌、打乱、恢复子系统和重启的回执丢失时，电脑端提示结果未知，由用户决定下一步。

| 能力 | 请求 | 成功回包业务字段 |
| --- | --- | --- |
| `cap_ext` | `capabilities_ext` | 返回 `hello` 容纳不下的扩展能力键值（值均为 `1`），由 `Channel.handshake()` 合并至 `channel.capabilities` |
| `battery_status` | `heartbeat` 或 `state` | 新增 `battery` 对象，例如 `{"mv":3920,"percent":null,"power":"unknown","low":false}`。未知读数用 `null`。`power` 取 `unknown`、`battery`、`charging` 或 `external_full`，后两者须满足板端实测判定条件 |
| `device_info`（扩展） | `device_info` | 沿用现有 `device_info: 1` 能力（不占用已用于 `network_status`/`card_status` 的 `device_diagnostics`）。电脑端白名单保留新增的 `diagnostics` 对象，例如 `{"reset_reason":"watchdog","early_resets":1,"safe_mode":false,"crash_present":true}`。`reset_reason` 是脱敏类别，无法识别时取 `unknown` |
| `subsystem_recover` | `subsystem_recover`，`target` 取 `sd`、`i2c`、`audio` 或 `wifi` | `{"status":"accepted"}` 表示板端已受理。用 `desk_diagnostics` 查看最近一次执行结果 |
| `system_restart` | `system_restart`，可选布尔字段 `confirm`。声明 `assistant_onboard` 的设备接收 Base 语音请求时还带当前录音编号 `ticket` | `desk_restart` 省略 `confirm`，板端有界收尾并返回 `{"status":"accepted"}`。语音工具固定传 `confirm: true`，`confirm_pending` 仅表示已请求屏上确认。板端须收到实际点按才重启 |
| `maintenance_restart_confirm` | 配对加密连接中的 `maintenance_restart_confirm`，仅含公共报文字段 | `desk_restart(confirm=true)` 优先使用此能力，返回 `{"status":"confirm_pending"}`。设备显示电脑端请求及 15 秒时限，单独确认后重启。未配对链路返回 `encrypted_link_required` |
| `device_settings` | `device_settings`。查询无业务字段。临时调整时传当前录音编号 `ticket`，以及 `volume`（0–100）和 `brightness`（10–100）中的至少一个 | 返回当前 `volume` 和 `brightness`。语音调整仅对当前会话生效 |
| `music_control` | `music_control`，`action` 取 `status`、`play`、`pause`、`stop`、`prev`、`next` 或 `shuffle`。`play` 可带相对路径 `path` | 返回操作后的 `state`（`playing`、`paused`、`stopped`）、`path`、`title`、`position_ms`、`volume`（0–100）和 `shuffled`。空曲库时 `path`、`title` 为 `null`。查询以外的失败回包不得报告成功 |
| `music_library` | `music_list`、`music_delete` | 见下节。导入须同时声明 `card_transfer`，复用已有上传及文件校验回执 |
| `capture_media_tools` | 带 `ticket` 的 `music_control` 或 `photo_album` | 只接受当前语音对话的执行端和已授权的工具。需要延后开始时返回 `queued:true`，音乐同时返回 `target_path` |
| `capture_voice_role` | 配对连接中的 `capture_role_view` 和 `capture_role_play` | 以当前语音 `ticket` 校验执行方和 `play_role_action` 授权。在从工作台进入的语音页确认角色已呈现后播放动作 |
| `capture_speech_cancel_ticket` | `speech_cancel`，可带非零无符号 32 位整数 `ticket` | 带编号时仅取消与该录音关联的播报。编号已失效时返回标准 `ok:true`，其余播报继续。省略编号沿用原有取消命令 |
| `capture_failure_detail` | 当前录音 `ticket` 的 `capture_result`，`success:false`，`failure` 为约定错误码 | 标准 `ok:true`。设备按当前识别、对话或播报阶段显示故障并保留诊断摘要 |
| `assistant_onboard` | `agent_config_begin`、`agent_config_part`、`agent_config_commit`、`agent_config_status`、`agent_config_cancel` | 见配置更新示例。电脑端入口为 `desk_agent_config` |
| `assistant_onboard` | `voice_permissions_set`，`enabled` 为完整工具名数组，例如 `{"enabled":["device_status"]}` | 板端在当前配对下完整保存后返回 `{"applied":true}`。空数组撤销全部授权。新配对同步前默认全关 |
| `agent_tool_callback` | `agent_tool_poll`、`agent_tool_result` | 经配对 Wi-Fi 将板载助手当前会话中的只读工具请求交给电脑端处理。依赖 `cap_ext` 与 `assistant_onboard` |

收到 `device_restarting` 表示板端已进入有界的重启收尾窗口。握手、能力、状态、诊断和已受理的传输及角色动作回执仍可查询，重复请求重启返回 `busy`。新的操作请求会被拒绝。设备重新上线后可查询状态，再决定是否重新发起操作。

收到 `low_battery_readonly` 表示低电保护已暂停新的 TF 卡写入。状态和素材列表仍可查询。电脑端保留消息同步状态，下一轮同步时再尝试写入和确认。已受理的后台上传若因低电收尾超时而取消，`card_fetch_status` 会在关闭连接和清理临时文件后返回同一个错误。清理失败仍返回 `card_transfer_failed`，并附 `card_cancel_close` 或 `card_cancel_cleanup`。ES3C35P 当前仅在诊断固件中验证该保护。

诊断摘要有可选的 `crash_pc` 和 `crash_lr`，分别为有效的崩溃指令地址和返回地址，采用无符号 32 位整数。可选的 `last_subsystem` 记录本次启动后最近一次完成的子系统操作，包含 `target` 和 `status`。状态取 `busy`、`failed`、`recovered` 或 `reconnecting`。Wi-Fi 的 `reconnecting` 表示已开始重连，连接结果仍由网络状态确认。安全模式可只提供 `device_info` 摘要。

软重启若在收尾限时结束时仍有保存工作待确认，下一次启动的 `diagnostics.restart_pending_writes` 提供类别列表。类别取 `card`（文件传输或已取得独占写卡权限的修改）、`inbox`（消息缓存写入）、`merit`（待保存的木鱼敲击）。有记录时且 `reset_reason` 为 `software` 才上报。各类别最多出现一次，供诊断时核对对应数据。

ES3C35P 当前上报滤波后的电池端电压。采样尚未稳定，或连续 6 秒没有有效读数时，`mv` 为 `null`。由于供电状态尚无独立证据，`power` 为 `unknown`，`percent` 为 `null`，`low` 为 `false`。电脑端 `desk_status` 按能力位展示摘要。

## 电脑端语音失败回执

设备在 `capabilities_ext` 中声明 `capture_failure_detail:1` 时，电脑端可在 `capture_result` 的 `success:false` 回执中传 `invalid_token`、`rate_limited`、`invalid_response`、`service_unreachable` 或 `service_timeout`。设备按录音编号校验当前轮次，并用当前处理阶段区分识别、对话和合成故障。传输只携带固定错误码。未声明该能力时沿用原来的失败回执和 `capture_no_speech` 能力门控。

```text
P1,{"epoch":"demo","seq":14,"type":"capture_result","ticket":7,"success":false,"failure":"service_timeout"}
P1,{"epoch":"demo","seq":14,"boot":4,"ok":true}
```

## 板载助手的电脑端工具回调

1. 电脑以配对加密 Wi-Fi 建立连接，完成能力握手和当前 `voice_permissions_set` 同步后，发送无业务字段的 `agent_tool_poll`。板端将该连接标为可接收回调，3 秒内未再次轮询则失效。断线或连接代际变更清除待处理请求。
2. 有待处理请求时，成功回包带 `call` 对象，例如 `{"call":{"ticket":7,"id":42,"name":"device_status"}}`。无待处理请求时省略 `call`。`ticket` 和 `id` 均使用非零无符号 32 位整数。`ticket` 绑定当前板载语音轮次。`id` 在一次板端启动中递增，回绕时跳过零。板端每次只交付一个请求，每个请求仅交付一次。
3. 工具限于 `device_status` 和 `list_role_actions`，沿用 Base 语音工具的结果字段，均无参数。电脑在执行和返回前复核当前配对、连接和授权。角色动作列表若超出报文预算，返回能够容纳的前缀并标记 `truncated:true`。
4. `agent_tool_result` 带同一组 `ticket`、`id`，以及布尔值 `success` 和 JSON 对象 `result`。`result` 经紧凑 ASCII JSON 编码后最多 800 字节，整条请求受 P1 1024 字节限制，回包受 1152 字节限制。板端在发起回调后最多等待 5 秒，只接收当前板载语音轮次和连接的结果。接收成功返回标准 `ok:true`。轮次已取消、结果超时或重复时返回 `callback_stale`，电脑无需重发。

## 曲库、路径规范、分页与删除

1. **目录映射与路径格式：**
   - 协议层相对路径统一使用小写前缀 `music/<文件名>` 和 `media/<文件名>`；板端与主机测试将其映射到储存卡根目录下的 `Music/` 和 `Media/` 目录（与现有 `photos/` 映射 `/sdcard/Photos`、`wallpapers/` 映射 `/sdcard/Wallpapers` 一致）。
   - 仅接受受控目录下的单个 `.mp3` 或 `.flac` 文件（不区分扩展名大小写，拒绝子目录及 `.wav` 等其他格式）。两目录合计最多 256 首，满额时导入返回 `music_library_full`。同一路径已有不同内容时返回 `music_immutable`。
2. **文件名字符集、加密 AAD 与长度限制：**
   - 电脑端和固件均接受有效 UTF-8 文件名。文件名须符合 TF 卡文件系统规则，禁止路径分隔符、控制字符（码点小于 32 或等于 127）、`< > : " | ? *`、点号头尾以及 `.upload`、`.previous`、`.removing` 保留后缀。
   - 单个文件名的 UTF-8 字节长度须为 **1–120 字节**（保证经 `channel.py` 的 `ensure_ascii=True` 转义为 `\uXXXX` 后，单条 `music_delete` 或 `music_control` 请求整行仍远低于 1024 字节）。
   - `card_upload.py` 与 `deskpet_card.c` 的 AES-GCM 附加认证数据（`desk-card-v1\n{path}\n{size}\n{crc:08x}`）统一按 **UTF-8** 字节编码计算（对既有纯 ASCII 路径完全兼容）。
3. **分页与游标失效：**
   - 电脑端对目录中的每个文件使用列表返回的完整相对路径。设备屏上确认删除与电脑端显式删除共用同一操作，语音工具集不提供删除。
   - 首个请求省略 `cursor`。`next_cursor` 是绑定本次启动和曲库排列版本的不透明字符串，末页取 `null`。设备按最终 JSON 编码后整行 **≤ 1152 字节**动态控制每页条数。上传、删除或打乱改变曲库排列后，旧游标返回 `stale_cursor`。电脑端丢弃已取的各页，从第一页重新读取。

```text
P1,{"epoch":"demo","seq":5,"type":"music_list"}
P1,{"epoch":"demo","seq":5,"boot":4,"ok":true,"items":[{"path":"music/sea.mp3"}],"next_cursor":"c7"}
P1,{"epoch":"demo","seq":6,"type":"music_delete","path":"music/sea.mp3"}
P1,{"epoch":"demo","seq":6,"boot":4,"ok":true,"deleted":true}
P1,{"epoch":"demo","seq":7,"type":"music_list","cursor":"c7"}
P1,{"epoch":"demo","seq":7,"boot":4,"ok":false,"error":"stale_cursor"}
P1,{"epoch":"demo","seq":8,"type":"music_list"}
P1,{"epoch":"demo","seq":8,"boot":4,"ok":true,"items":[{"path":"media/rain.flac"}],"next_cursor":null}
```

删除合法但已不存在的路径返回 `deleted=false`。越出受控目录、文件名超长或格式不符返回 `invalid_path`，实际写卡失败返回 `card_io`。导入遇到不支持的格式时，在创建文件前拒绝。

## 语音控制音乐与相册

- 有 `capture_media_tools:1` 及对应媒体能力时，电脑端语音可在授权后复用现有命令，并附当前对话的整数 `ticket`。板载直连使用同名、同参数的工具和同一板端执行入口。语音仅转写、过期会话和未获授权的工具均无法执行。
- 语音 `music_control` 接受 `status`、`play`、`pause`、`stop`、`prev`、`next` 和 `shuffle`。只有 `play` 可选填曲库已有的 `path`。播放和切歌回包中的 `state` 等字段反映接收时状态，`queued:true` 与 `target_path` 表示待播放目标。暂停和停止立即生效，可取消待播放目标。打乱顺序沿用曲库持久化规则。
- 语音 `photo_slideshow` 接受 `play`、`pause` 和 `interval`，通过 `photo_album.mode` 传输。仅 `interval` 需要 `interval_seconds`（P1 字段是 `seconds`），取 5、10 或 15。语音调整立即应用，设备重启后恢复已保存的间隔。轮播开始回包为 `queued:true`。
- 待播放目标在本轮语音回复完整播完后生效。用户取消、回复失败或工具授权失效时丢弃待播放目标。回执未送达时电脑端提示结果未知，可先查询设备状态。

## 语音播放角色动作

`capture_role_view` 只传当前 `ticket`。成功回包的 `role_view` 为 `pending` 或 `visible`。电脑端查询当前角色的动作列表后，等待 `visible` 再发送 `capture_role_play`。后者携带同一 `ticket`、动作的零基 `action` 序号和 `duration_ms`。时长可为 100 至 6,000 毫秒，0 沿用动作时长。板端校验列表与屏上角色对应同一次加载，旧序号无法用于已经切换的角色。

`action_status` 可以是 `started`、`busy`、`unsupported` 或 `unavailable`。`started` 回包带 `operation`，可与回包的 `boot` 一起用于 `role_action_status`。板载助手用同名 `play_role_action` 工具按动作名找到当前角色的动作，共用语音页和播放控制器。退出语音页、取消、断线或撤销授权会终止对应动作。丢失播放回执时，电脑端提示结果未知并结束本轮工具调用。

```text
P1,{"epoch":"demo","seq":9,"type":"music_control","action":"play","path":"music/sea.mp3","ticket":42}
P1,{"epoch":"demo","seq":9,"boot":4,"state":"stopped","path":null,"title":null,"position_ms":0,"volume":70,"shuffled":false,"queued":true,"target_path":"music/sea.mp3","ok":true}
P1,{"epoch":"demo","seq":10,"type":"photo_album","mode":"interval","seconds":15,"ticket":42}
P1,{"epoch":"demo","seq":10,"boot":4,"view":"closed","playing":false,"interval_seconds":15,"count":0,"page":0,"pages":0,"photos":[],"photo_drawn":false,"photo_draws":0,"ok":true}
```

## 语音查询天气与额度

已配对的加密 Wi-Fi 会话通过 `capabilities_ext` 按项协商 `weather_status` 和 `quota_status`。同名查询命令的业务参数为空。Base 语音和板载助手沿用各自的工具授权，对设备工作台的同一份数据执行只读查询。

- `weather_status` 在 `status:"ok"` 时提供摄氏温度 `degrees_c`、天气类别 `condition`，以及已知时的 `city`。天气类别取 `clear`、`clear_night`、`cloudy`、`rain`、`snow`、`fog`、`thunderstorm`、`partly_cloudy` 或 `unknown`。数据过期、定位或获取中、缺少数据时分别返回 `stale`、`pending`、`unavailable`。已过期的转发天气可由板载天气替代。缺少板载天气时报告 `stale`。
- `quota_status` 按 `codex`、`claude_code`、`antigravity_gemini`、`antigravity_third_party` 的顺序返回 `sources`。每项含 `source` 和 `status`。只有 `ok` 的来源返回 `windows`，每个窗口包含 `remaining_percent`、`window_minutes` 和 `reset_at`。百分比 0 表示额度用完。重置时间未知时 `reset_at` 为 `null`。查询时检查各窗口的重置时间。旧版 Codex 快照若有重复周期，取当前仍有效的条目中剩余比例较小的一项。过期的来源返回 `stale`，等待对时的来源返回 `clock_pending`，缺少数据的来源返回 `unavailable`。顶层状态按 `ok`、`stale`、`clock_pending`、`unavailable` 的顺序取第一个存在的状态。板载助手在电脑断线时收到 `stale`。

## 配置更新与结果查询

一次配置修改使用一个客户端生成的唯一 `tx`，以经过认证的配对和当前连接为边界。`begin` 声明 UTF-8 JSON 补丁的总字节数 `size`，取值 1–4096。每个 `part` 分别用标准 Base64 编码，`offset` 按解码后的字节从 0 连续递增。`next_offset` 返回已接收的字节数。板端每次只暂存一笔修改。缺段、越界或超时即丢弃本次暂存，断线也清理未提交内容。相应错误码为 `invalid_offset`、`invalid_size`、`tx_expired`。未知交易返回 `unknown_tx`。

Platform 使用 32 字符的交易标识，每部分最多 600 原始字节。`agent_config_cancel` 带 `tx`，返回 `cancelled: true` 时已丢弃该交易尚未提交的数据。

`commit` 时校验完整补丁、合并后的配置及容量，只替换补丁中出现的字段。保存成功才返回 `applied=true`，并将 `last_applied_tx` 与配置一并持久化。重连后先查状态再决定是否重新配置。查询返回生效模式、最近成功的交易标识和各组令牌是否已设置。令牌仅在配置写入时传输。

`desk_agent_config` 不带 `config` 时查询。写入和权限同步使用配对后的加密 Wi-Fi 连接。`config` 是字段补丁，例如 `{"mode":"platform"}`。可用字段见下表。

| 字段 | 内容 |
| --- | --- |
| `mode` | `platform`、`onboard` 或 `auto` |
| `chat` | `url`、`token`、`model`、`tool_format`（`native` 或 `json`） |
| `asr` | `url`、`token`、`model`。空地址和空令牌沿用对话组 |
| `tts` | `url`、`token`、`model`、`voice` |
| `persona` | `prompt`、`user_name`、`device_name` |
| `wake_enabled` | 布尔值 |

按 UTF-8 计算长度。地址、令牌、模型名的上限分别为 256、512、128 字节。提示词最多 1024 字节，音色和各昵称最多 64 字节。地址使用 HTTPS，私网 IPv4 和 IPv6 ULA 字面量可使用 HTTP。提交回执丢失时，Platform 只读取状态并用 `last_applied_tx` 核对，无法核对时返回 `status: "unknown"` 和原 `tx`。

```text
P1,{"epoch":"demo","seq":9,"type":"agent_config_begin","tx":"t42","size":15}
P1,{"epoch":"demo","seq":9,"boot":4,"ok":true,"next_offset":0}
P1,{"epoch":"demo","seq":10,"type":"agent_config_part","tx":"t42","offset":0,"data":"eyJtb2RlIjo="}
P1,{"epoch":"demo","seq":10,"boot":4,"ok":true,"next_offset":8}
P1,{"epoch":"demo","seq":11,"type":"agent_config_part","tx":"t42","offset":8,"data":"ImF1dG8ifQ=="}
P1,{"epoch":"demo","seq":11,"boot":4,"ok":true,"next_offset":15}
P1,{"epoch":"demo","seq":12,"type":"agent_config_commit","tx":"t42"}
P1,{"epoch":"demo","seq":12,"boot":4,"ok":true,"applied":true}
P1,{"epoch":"demo","seq":13,"type":"agent_config_status"}
P1,{"epoch":"demo","seq":13,"boot":4,"ok":true,"mode":"auto","last_applied_tx":"t42","configured":{"chat":false,"asr":false,"tts":false}}
```

提交前发现缺段时，例如已收到 8 字节却收到 `offset=9` 的片段，失败回包为 `{"epoch":"demo","seq":11,"boot":4,"ok":false,"error":"invalid_offset"}`。

联调时使用两段以上的合法配置，分别在最后一段前和 `commit` 回执前断线。前者重连查询须看到旧配置，后者按 `last_applied_tx` 核对实际生效的交易。请求或响应超过上限时应在各自编码边界被拒绝。
