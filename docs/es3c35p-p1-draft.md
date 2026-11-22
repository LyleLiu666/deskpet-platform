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
| `device_settings` | `device_settings`。查询无业务字段。临时调整时传当前录音编号 `ticket`，以及 `volume`（0–100）和 `brightness`（10–100）中的至少一个 | 返回当前 `volume` 和 `brightness`。语音调整仅对当前会话生效 |
| `music_control` | `music_control`，`action` 取 `status`、`play`、`pause`、`stop`、`prev`、`next` 或 `shuffle`。`play` 可带相对路径 `path` | 返回操作后的 `state`（`playing`、`paused`、`stopped`）、`path`、`title`、`position_ms`、`volume`（0–100）和 `shuffled`。空曲库时 `path`、`title` 为 `null`。查询以外的失败回包不得报告成功 |
| `music_library` | `music_list`、`music_delete` | 见下节。导入须同时声明 `card_transfer`，复用已有上传及文件校验回执 |
| `capture_media_tools` | 带 `ticket` 的 `music_control` 或 `photo_album` | 只接受当前语音对话的执行端和已授权的工具。需要延后开始时返回 `queued:true`，音乐同时返回 `target_path` |
| `assistant_onboard` | `agent_config_begin`、`agent_config_part`、`agent_config_commit`、`agent_config_status`、`agent_config_cancel` | 见配置更新示例。电脑端入口为 `desk_agent_config` |
| `assistant_onboard` | `voice_permissions_set`，`enabled` 为完整工具名数组，例如 `{"enabled":["device_status"]}` | 板端在当前配对下完整保存后返回 `{"applied":true}`。空数组撤销全部授权。新配对同步前默认全关 |

收到 `device_restarting` 表示板端已进入有界的重启收尾窗口。握手、能力、状态、诊断和已受理的传输及角色动作回执仍可查询，重复请求重启返回 `busy`。新的操作请求会被拒绝。设备重新上线后可查询状态，再决定是否重新发起操作。

诊断摘要有可选的 `crash_pc` 和 `crash_lr`，分别为有效的崩溃指令地址和返回地址，采用无符号 32 位整数。可选的 `last_subsystem` 记录本次启动后最近一次完成的子系统操作，包含 `target` 和 `status`。状态取 `busy`、`failed`、`recovered` 或 `reconnecting`。Wi-Fi 的 `reconnecting` 表示已开始重连，连接结果仍由网络状态确认。安全模式可只提供 `device_info` 摘要。

软重启若在收尾限时结束时仍有保存工作待确认，下一次启动的 `diagnostics.restart_pending_writes` 提供类别列表。类别取 `card`（文件传输或已取得独占写卡权限的修改）、`inbox`（消息缓存写入）、`merit`（待保存的木鱼敲击）。有记录时且 `reset_reason` 为 `software` 才上报。各类别最多出现一次，供诊断时核对对应数据。

ES3C35P 当前上报滤波后的电池端电压。采样尚未稳定，或连续 6 秒没有有效读数时，`mv` 为 `null`。由于供电状态尚无独立证据，`power` 为 `unknown`，`percent` 为 `null`，`low` 为 `false`。电脑端 `desk_status` 按能力位展示摘要。

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

```text
P1,{"epoch":"demo","seq":9,"type":"music_control","action":"play","path":"music/sea.mp3","ticket":42}
P1,{"epoch":"demo","seq":9,"boot":4,"state":"stopped","path":null,"title":null,"position_ms":0,"volume":70,"shuffled":false,"queued":true,"target_path":"music/sea.mp3","ok":true}
P1,{"epoch":"demo","seq":10,"type":"photo_album","mode":"interval","seconds":15,"ticket":42}
P1,{"epoch":"demo","seq":10,"boot":4,"view":"closed","playing":false,"interval_seconds":15,"count":0,"page":0,"pages":0,"photos":[],"photo_drawn":false,"photo_draws":0,"ok":true}
```

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
