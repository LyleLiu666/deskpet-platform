# DeskPet 二次开发手册

用 Python SDK 建立连接，显示自己的短消息。直接使用 SDK 时，先关闭同一小板的 MCP 实例。

```python
import asyncio
from deskpet_platform.client import Client
from deskpet_platform.pairing import DEFAULT_CONFIG, DeviceConfig

async def main():
    async with Client(DeviceConfig.load(DEFAULT_CONFIG)) as board:
        print(await board.status())
        print(await board.show_text("Hello from my own app"))

asyncio.run(main())
```

- `Client` 在进入上下文时完成 Wi‑Fi 发现、双向认证和 P1 握手。退出时释放连接。长期运行的扩展需定期发心跳并在掉线时重新建立会话。底座的 MCP 进程会处理这两件事。
- 多台设备可用 `DeviceRegistry(DEFAULT_CONFIG).list()` 查询，再以列表中的 `id` 调用 `DeviceRegistry(DEFAULT_CONFIG).get(id)`，把得到的配置交给对应的 `Client`。每台设备各自持有连接；带 UUID 的设备握手时必须与已保存的 UUID 一致。旧固件没有 UUID 时暂用 `mac:` 身份，不会在电脑端伪造 UUID。
- `Channel` 是协议层。命令在同一连接中串行执行，通过 epoch、seq 和 boot 对应回执。超时后重新连接。有副作用的命令在结果不明时由上层决定如何恢复。
- `Client.show_text` 将短文字绘成位图；支持电脑可用字体中的字符，最多两行。显示回执区分接受和已实际绘制。
- `Client.show_image` 转换图片并传入储存卡；`Client.upload_file` 可传输指定文件。角色安装、查询、切换和动作可通过 MCP 工具调用。
- 固件声明 `role_preview` 能力时，可通过 `Client.role_preview` 或 MCP 的 `desk_role_preview` 控制图鉴试播。`start` 返回 `loading`，随后用 `status` 查询 `playing` 和当前阶段。`use` 只在播放期间启用角色，`cancel` 可结束加载或播放。
- 固件声明 `photo_album` 能力时，`Client.photo_album` 与 `desk_photo_album` 共用相册操作。网格返回当前页的照片标识和成功绘制的缩略图数，照片视图返回当前标识及成功绘制全图的次数。
- 操作手册、二次开发手册和自定义角色指南既是资源，也是 `desk_read_manual` 工具的返回内容。制作图鉴角色可读 `deskpet://manual/roles`。
- 用 `uv run --locked python -m unittest discover -s tests` 执行本地测试。测试设备使用合成凭据和本机回环网络。

支持触摸回传的固件可以使用 `await board.next_touch(timeout=30)`。它返回已确认的点击事件。可以将该事件接到你自己的自动化流程，或随后用 `show_text` 在板上回应。样例位于公开仓库的 `examples/tap.py`。

## 协议概览

- 支持主动连接的固件在 UDP 34915 响应 `DESK-DIRECT-1,<设备标识>,<随机挑战>`。电脑校验回复中的 HMAC，再向搭搭的 TCP 34915 发起连接。
- 兼容旧固件时，电脑向 UDP 34915 端口发送 `DESK-LINK-1,<设备标识>,<本机临时 TCP 端口>`，搭搭回连。两种方向沿用同一认证过程。H1 / D1 / H2 交换随机值和 HMAC-SHA256 证明，再从长期密钥和两端随机值派生会话密钥。
- 后续的 TCP 记录是 `F,<序号>,<十六进制密文及认证标签>`。两方向各自从 1 开始计数。AES-GCM 的 nonce 为四字节方向标识加八字节大端序号，附加认证数据是 `desk-link-v1`。认证失败、跳号或超时后重新建链。具体编码见电脑端的 `wifi_secure.py`。
- 记录内承载逐行的 `P1,<JSON>`。根据板端握手声明的能力，SDK 提供储存卡传输、角色、图片、亮度及触摸校准命令。消息字段可查看电脑端的 `client.py` 和 `channel.py`。
- 固件声明 `card_fetch_receipt` 时，MCP 可在 Wi-Fi 控制连接中断后重新连接同一台设备，按 `transfer_id` 查询原写卡任务的回执。旧固件继续使用当前连接的 `operation` 查询。
- 使用 `desk_render_metrics(reset=true)` 开始采样，让同一角色、同一档位播放固定时长，再调用 `desk_render_metrics`。旧版的 `fps` 按采样窗口内处理的帧数计算。新版的 `displayed_fps` 只计算至少有一个动画分块写入屏幕的帧，`displayed_frame_gap_ms` 可查看写屏帧之间的最长间隔。
- `load_us`、`background_us` 和 `composite_us` 记录读帧、背景处理和合成。`background_cached` 表示当前角色的背景是否已缓存。`display_us` 包括提交分块时的触摸采样与像素比较，`spi_us` 只计屏幕写入。
- `touch_dispatch_ms` 从固件首次采样到抬手起算，到处理点击为止。`touch_role_feedback_ms` 从同一时刻起算，到本地 `touched` 动作首帧写屏完成为止。没有写屏的动作不计入反馈时间。`touch_ack_ms` 从设备发出点击事件到收到电脑回执。
- 串口 115200 支持 `PERF RESET` 和 `PERF?`，各耗时数组依次为样本数、总耗时和最大耗时。
