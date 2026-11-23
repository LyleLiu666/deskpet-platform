# DeskPet 二次开发手册

通过 `deskpet_platform` Python SDK，你可以在自己的脚本或应用中直接连接已配对的托肯（DeskPet），查询状态、推送文字与图片、监听触摸事件或管理相册与角色。

> **注意**：一台托肯同一时间只能保持一个控制连接。直接使用 Python SDK 连接前，请先关闭控制同一台托肯的 MCP 会话或常驻服务。

## Python SDK 快速上手

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

- **连接生命周期**：`Client` 在进入 `async with` 上下文时自动完成局域网 UDP 发现、双向认证和 `P1` 协议握手，退出上下文时释放连接。长期运行的自定义程序需定期发送心跳并在断线后重建会话（内置的 MCP 进程与 `service` 常驻服务已自动封装心跳与重连）。
- **多台托肯管理**：配对了多台设备时，先通过 `DeviceRegistry(DEFAULT_CONFIG).list()` 获取设备列表，再用列表中的 `id` 调用 `DeviceRegistry(DEFAULT_CONFIG).get(id)` 取得对应 `DeviceConfig` 并传给各自的 `Client`。每台设备各自独立持有连接；支持持久 UUID 的设备在握手时必须与本机保存的 UUID 一致（旧固件尚无 UUID 时暂用 `mac:` 标识，电脑端不会伪造 UUID）。
- **协议通道层（`Channel`）**：命令在同一连接内串行执行，通过 `epoch`、`seq` 和 `boot` 匹配回执，超时后自动断开重连。带有写卡等副作用的命令若在连接中断时结果不明，由上层调用方结合设备状态决定恢复策略。

## 常用 SDK 接口与 MCP 资源

- **文字与图片显示**：
  - `Client.show_text(text)`：将不超过两行的短文字用电脑可用字体（或随包 Noto Sans CJK SC 字体）渲染为位图发送到托肯；返回回执区分「已接收」与「已实际绘制上屏」。
  - `Client.show_image(source)`：将图片转换为适合屏幕的格式并写入储存卡展示。
  - `Client.upload_file(...)`：向储存卡传输指定文件。角色校验、安装、查询、切换和动作播放建议直接通过 MCP 工具或对应模块调用。
- **触摸事件回传**：
  支持触摸回传的固件可调用 `await board.next_touch(timeout=30)` 等待并获取已确认的屏幕点击事件，将其接入你自己的自动化流程，或随后调用 `show_text` 在屏幕上给出反馈（完整示例见仓库 `examples/tap.py`，状态查询示例见 `examples/status.py`）。
- **角色图鉴试播与相册控制**：
  - 固件声明 `role_preview` 能力时，可通过 `Client.role_preview`（或 MCP 的 `desk_role_preview`）控制图鉴试播：`start` 返回 `loading`，随后用 `status` 查询 `playing` 与当前阶段；播放期间可用 `use` 直接启用该角色，或用 `cancel` 终止加载/播放。
  - 固件声明 `photo_album` 能力时，`Client.photo_album`（或 MCP 的 `desk_photo_album`）共用相册操作：网格视图返回当前页的照片标识与成功绘制的缩略图数量，单张照片视图返回当前照片标识与全图成功绘制次数。
- **内置手册资源**：
  操作手册（`operations`）、二次开发手册（`development`）和自定义角色指南（`roles`）既可通过 MCP 工具 `desk_read_manual(topic=...)` 读取，也注册为 MCP 资源（如 `deskpet://manual/roles`）。

## 局域网与串口通信协议

1. **局域网发现与连接方向**：
   - **电脑主动连接（当前标准固件）**：电脑向局域网 UDP `34915` 端口发送 `DESK-DIRECT-1,<设备标识>,<随机挑战>`，校验托肯回复中的 HMAC 后，由电脑主动连接托肯的 TCP `34915` 端口。
   - **设备回连（兼容旧版固件）**：电脑向 UDP `34915` 端口发送 `DESK-LINK-1,<设备标识>,<本机临时 TCP 端口>`，由托肯回连电脑。
2. **双向认证与会话加密**：
   - 两种连接方向共用同一套 `H1 / D1 / H2` 握手流程：双方交换随机值与 `HMAC-SHA256` 证明，并由配对长期密钥和两端随机值派生本次会话密钥。
   - 握手后的 TCP 帧格式为 `F,<序号>,<十六进制密文及认证标签>`，收发双方各自从 `1` 开始递增计数。
   - 加密采用 `AES-GCM`：Nonce 由 4 字节方向标识加 8 字节大端序号组成，附加认证数据（AAD）固定为 `desk-link-v1`。一旦认证失败、序号跳号或超时，立即断开并重新建链（具体实现见 `src/deskpet_platform/wifi_secure.py`）。
3. **应用层命令与写卡断点回执**：
   - 密文解密后承载逐行的 `P1,<JSON>` 载荷。SDK 根据设备握手时声明的 `capabilities` 提供储存卡传输、角色、图片、亮度及触摸校准等命令（字段定义见 `src/deskpet_platform/client.py` 与 `channel.py`）。
   - 固件声明 `card_fetch_receipt` 时，若 Wi-Fi 控制连接在写卡期间中断，电脑重连同一台设备后可按 `transfer_id` 查询原写卡任务的完成回执，避免重复重传已落盘文件（旧固件继续按当前连接的 `operation` 查询）。
   - 固件声明 `speed_burst` 时，电脑在 Token 速度突增期间随 `token_update` 附带 `burst_ms`（本次爆气已持续的毫秒数，0–4999）和可选的 `surge`（相对突增前均值的涨幅百分比，最大 999）。判定规则与参考固件一致：处于第四档及以上时，连续两次读数达到前五次均值的 120%，爆气持续 5 秒，之后 20 秒内不再触发。设备据此播放爆气并绘制特效，不需要自行判定。同时附带 `speed_level`（整体四档 0–3，未校准时为 0），设备按它显示灰、白、黄、青蓝四种颜色。

## 渲染与触摸性能采样

- **采样方法**：调用 `desk_render_metrics(reset=true)` 清空采样窗口，让同一角色在同一档位下持续播放固定时长，再调用 `desk_render_metrics` 读取统计数据（串口 `115200` 波特率下也支持发送 `PERF RESET` 和 `PERF?` 查询，各耗时数组依次为 `[样本数, 总耗时, 最大耗时]`）。
- **帧率指标**：
  - `displayed_fps`：仅统计至少有一个动画分块实际写入屏幕的帧率；`displayed_frame_gap_ms` 记录实际写屏帧之间的最长间隔（毫秒）。
  - `fps`（旧版兼容字段）：按采样窗口内处理的全部帧数计算。
- **阶段耗时（微秒）**：
  - `load_us`、`background_us`、`composite_us` 分别记录读卡解帧、背景处理和图层合成耗时；`background_cached` 表示当前角色背景是否已常驻内存缓存。
  - `display_us` 包含提交分块期间的触摸采样与像素差异比较耗时；`spi_us` 仅统计实际 SPI 写屏耗时。
- **触摸响应耗时（毫秒）**：
  - `touch_dispatch_ms`：从固件首次采样到抬手时刻起算，至完成点击事件分发为止。
  - `touch_role_feedback_ms`：从同一抬手时刻起算，至本地 `touched` 互动动作首帧写屏完成为止（未发生写屏的动作不计入）。
  - `touch_ack_ms`：从设备发出点击事件至收到电脑端回执的往返耗时。

## 本地测试

在仓库根目录下运行以下命令执行完整单元测试（测试使用合成凭据与本机回环网络，无需连接真实硬件）：

```sh
uv run --locked python -m unittest discover -s tests
```
