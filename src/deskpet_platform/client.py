"""Connection, status and a small text display operation for developers."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import hashlib
from pathlib import Path
import re
import tempfile
import zlib

from PIL import Image, ImageDraw, ImageFont

from .adaptive_speed.config import Config as SpeedConfig
from .card_upload import upload_file, upload_usb, validate_path
from .channel import Channel, CommandRejected, DeviceError, TextNotVisible
from .image_display import prepare_image
from .pairing import DeviceConfig, valid_device_uuid
from .quota_projection import SOURCES
from .role_install import MAX_ROLE_ACTIONS, MAX_ROLE_ID_LENGTH, valid_identity
from .wifi_link import WifiLink

SCREEN_PAGES = ("role", "home", "clock", "settings", "roles", "photos", "weather", "messages")
DEFAULT_SCREEN_PAGES = ("role", "home", "clock", "photos")


class Client:
    """One authenticated board connection. Use as an asynchronous context manager."""

    def __init__(self, config: DeviceConfig, *, link_factory=WifiLink):
        self.config = config
        self.link = link_factory(config.identity, config.key, peer_hint=config.address)
        self.channel = Channel(self.link, config.identity)
        self._operation = asyncio.Lock()
        self._upload = asyncio.Lock()
        self.device_id = None

    async def __aenter__(self):
        self.device_id = None
        try:
            await self.link.open()
            capabilities = await self.channel.handshake()
            observed = capabilities.get("device_id")
            if "device_id" in capabilities and not valid_device_uuid(observed):
                raise DeviceError("invalid_device_uuid")
            if self.config.device_id is not None and observed != self.config.device_id:
                raise DeviceError("device_identity_mismatch")
            self.device_id = observed
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        self.channel.online = False
        await self.link.close()

    async def status(self, *, compact=False):
        async with self._operation:
            fields = {"compact": True} if compact and self.channel.capabilities.get("compact_status") == 1 else {}
            return await self.channel.send("heartbeat", **fields)

    async def render_metrics(self, *, reset=False):
        if self.channel.capabilities.get("render_metrics") != 1:
            raise DeviceError("render_metrics_not_supported")
        async with self._operation:
            return await self.channel.send("render_metrics", reset=reset)

    async def sync_clock(self, offset_minutes=None, *, only_missing=False):
        if self.channel.capabilities.get("clock_sync") != 1:
            raise DeviceError("clock_sync_not_supported")
        now = datetime.now().astimezone()
        if offset_minutes is None:
            offset_minutes = int(now.utcoffset().total_seconds() // 60)
        if type(offset_minutes) is not int or not -720 <= offset_minutes <= 840:
            raise ValueError("时区偏移须在 -720 到 840 分钟之间。")
        fields = {"unix_seconds": int(now.timestamp()), "offset_minutes": offset_minutes}
        if only_missing:
            fields["only_missing"] = True
        async with self._operation:
            return await self.channel.send("clock_set", **fields)

    async def clock_edit(self, mode="status", *, field=None, step=None):
        if self.channel.capabilities.get("clock_edit") != 1:
            raise DeviceError("clock_edit_not_supported")
        fields = {}
        if mode == "adjust":
            if field not in ("year", "month", "day", "hour", "minute") or type(step) is not int or step not in (-1, 1):
                raise ValueError("请选择要调整的日期或时间字段，步长为 -1 或 1。")
            fields = {"field": field, "step": step}
        elif mode not in ("status", "start", "save", "cancel") or field is not None or step is not None:
            raise ValueError("只有调整时钟时需要填写字段和步长。")
        async with self._operation:
            return await self.channel.send("clock_edit", mode=mode, **fields)

    async def set_view(self, page, *, make_default=False, clock_face=None):
        if self.channel.capabilities.get("screen_views") != 1:
            raise DeviceError("screen_views_not_supported")
        if page not in SCREEN_PAGES or (make_default and page not in DEFAULT_SCREEN_PAGES):
            raise ValueError("该页面不可用作指定的视图。")
        options = {}
        if clock_face is not None:
            if type(clock_face) is not int or not 0 <= clock_face < 6:
                raise ValueError("表盘编号须为 0 到 5。")
            options["clock_face"] = clock_face
        async with self._operation:
            return await self.channel.send("screen_view", page=SCREEN_PAGES.index(page),
                                           make_default=make_default, **options)

    async def photo_album(self, mode="status", *, photo=None, interval_seconds=None):
        if self.channel.capabilities.get("photo_album") != 1:
            raise DeviceError("photo_album_not_supported")
        if mode not in ("status", "grid", "open", "previous", "next", "play", "pause", "interval"):
            raise ValueError("相册操作无效。")
        fields = {}
        if mode == "open":
            if not isinstance(photo, str) or not re.fullmatch(r"p-[0-9a-f]{16}", photo):
                raise ValueError("请选择相册中的照片标识。")
            fields["id"] = photo
        elif photo is not None:
            raise ValueError("只有打开照片时需要填写照片标识。")
        if mode == "interval":
            if type(interval_seconds) is not int or interval_seconds not in (5, 10, 15):
                raise ValueError("轮播间隔须为 5、10 或 15 秒。")
            fields["seconds"] = interval_seconds
        elif interval_seconds is not None:
            raise ValueError("请选择设置轮播间隔操作。")
        async with self._operation:
            return await self.channel.send("photo_album", mode=mode, **fields)

    async def set_weather(self, snapshot, *, location=None):
        if self.channel.capabilities.get("weather") != 1:
            raise DeviceError("weather_not_supported")
        if location is not None and self.channel.capabilities.get("weather_device") == 1:
            snapshot = {**snapshot, "location": {key: location[key] for key in ("latitude", "longitude")}}
        async with self._operation:
            return await self.channel.send("weather_set", **snapshot)

    async def relay_weather(self, snapshot):
        if self.channel.capabilities.get("weather_relay") != 1:
            raise DeviceError("weather_not_supported")
        async with self._operation:
            return await self.channel.send("weather_relay", **snapshot)

    async def speed_windows(self, recent_seconds=None, long_days=None):
        if self.channel.capabilities.get("speed_windows") != 1:
            raise DeviceError("speed_windows_not_supported")
        fields = {}
        if recent_seconds is not None or long_days is not None:
            try:
                fields["speed_windows"] = asdict(SpeedConfig(recent_seconds, long_days))
            except ValueError as error:
                raise ValueError("请同时提供近期窗口和长期窗口。近期可选 120、300、600、1800、3600 秒，长期可选 1、3、7 天。") from error
        async with self._operation:
            return await self.channel.send("speed_windows", **fields)

    async def update_tokens(self, snapshot, *, windows=None):
        if self.channel.capabilities.get("token_stats") != 1:
            raise DeviceError("token_stats_not_supported")
        async with self._operation:
            if windows is not None:
                return await self.channel.send("token_update", **snapshot, speed_windows=windows)
            return await self.channel.send("token_update", **snapshot)

    async def update_quota(self, reading):
        if self.channel.capabilities.get("quota_sources") != 1:
            raise DeviceError("quota_sources_not_supported")
        source = reading.get("source")
        if source not in SOURCES:
            raise ValueError("额度来源无效。")
        fields = {key: value for key, value in reading.items() if key != "source"}
        async with self._operation:
            return await self.channel.send("quota_snapshot", source=SOURCES.index(source), **fields)

    async def select_quota_source(self, source):
        if self.channel.capabilities.get("quota_sources") != 1:
            raise DeviceError("quota_sources_not_supported")
        if source not in SOURCES:
            raise ValueError("额度来源无效。")
        async with self._operation:
            return await self.channel.send("quota_source", source=SOURCES.index(source))

    async def set_role_auto(self, enabled):
        if self.channel.capabilities.get("token_stats") != 1:
            raise DeviceError("token_stats_not_supported")
        async with self._operation:
            return await self.channel.send("token_auto", enabled=enabled)

    async def update_computer_metrics(self, *, cpu_enabled, memory_enabled, cpu, memory):
        if self.channel.capabilities.get("computer_metrics") != 1:
            raise DeviceError("computer_metrics_not_supported")
        if type(cpu_enabled) is not bool or type(memory_enabled) is not bool:
            raise ValueError("CPU 和内存显示开关须为开或关。")
        for enabled, value in ((cpu_enabled, cpu), (memory_enabled, memory)):
            if (not enabled and value is not None or
                    value is not None and (type(value) is not int or not 0 <= value <= 100)):
                raise ValueError("电脑使用率须为 0 至 100，或暂时未知。")
        async with self._operation:
            return await self.channel.send("computer_metrics", cpu_enabled=cpu_enabled,
                                           memory_enabled=memory_enabled, cpu=cpu, memory=memory)

    async def set_computer_preferences(self, *, cpu_enabled, memory_enabled):
        if self.channel.capabilities.get("computer_preferences") != 1:
            raise DeviceError("computer_preferences_not_supported")
        if type(cpu_enabled) is not bool or type(memory_enabled) is not bool:
            raise ValueError("CPU 和内存显示开关须为开或关。")
        async with self._operation:
            return await self.channel.send("computer_preferences", cpu_enabled=cpu_enabled, memory_enabled=memory_enabled)

    async def acknowledge_computer_preferences(self, state):
        if self.channel.capabilities.get("computer_preferences") != 1:
            raise DeviceError("computer_preferences_not_supported")
        if type(state) is not int or not 0 <= state <= 0xffffffff:
            raise ValueError("设备设置状态无效。")
        async with self._operation:
            return await self.channel.send("computer_prefs_ack", state=state)

    async def diagnostics(self, *, include_card=True):
        if self.channel.capabilities.get("device_diagnostics") != 1:
            raise DeviceError("device_diagnostics_not_supported")
        async with self._operation:
            reply = await self.channel.send("network_status")
            keys = ("transport", "uptime_ms", "reset_reason", "wifi_connected", "ssid", "rssi", "saved_networks",
                    "network_switch")
            result = {key: reply[key] for key in keys if key in reply}
            if self.channel.capabilities.get("network_profiles_hex") == 1:
                if "ssid_hex" in reply:
                    result["ssid"] = self._network_name(reply["ssid_hex"])
                if "network_switch" in result:
                    switch = dict(result["network_switch"])
                    if "target_hex" in switch:
                        switch["target"] = self._network_name(switch.pop("target_hex"))
                    result["network_switch"] = switch
                count = reply.get("saved_count")
                if type(count) is not int or not 0 <= count <= 16:
                    raise DeviceError("invalid_network_profiles_response")
                names = []
                seen = set()
                while len(names) < count:
                    page = await self.channel.send("network_profiles", offset=len(names))
                    values = page.get("saved_networks_hex")
                    end = page.get("next_offset")
                    if (page.get("saved_count") != count or not isinstance(values, list) or
                            not 1 <= len(values) <= 8 or type(end) is not int or
                            end != len(names) + len(values) or end > count):
                        raise DeviceError("invalid_network_profiles_response")
                    names.extend(self._network_name(value) for value in values)
                    seen.update(values)
                    if len(seen) != len(names):
                        raise DeviceError("invalid_network_profiles_response")
                result["saved_networks"] = names
            if include_card:
                try:
                    card = await self.channel.send("card_status")
                    result["card"] = {key: card[key] for key in
                                      ("card_ready", "total_bytes", "used_bytes", "spi_hz", "mount_count")
                                      if key in card}
                except CommandRejected as error:
                    if error.code != "card_busy":
                        raise
                    result["card"] = {"status": "busy"}
            return result

    @staticmethod
    def _network_name(value):
        if (not isinstance(value, str) or not 2 <= len(value) <= 64 or len(value) % 2 or
                any(char not in "0123456789abcdef" for char in value)):
            raise DeviceError("invalid_network_profiles_response")
        return bytes.fromhex(value).decode("utf-8", errors="replace")

    async def select_network(self, ssid):
        if self.channel.capabilities.get("device_diagnostics") != 1:
            raise DeviceError("device_diagnostics_not_supported")
        if not 1 <= len(ssid.encode("utf-8")) <= 32 or "\0" in ssid:
            raise ValueError("请提供已保存的 Wi-Fi 名称。")
        async with self._operation:
            return await self.channel.send("network_select", ssid=ssid)

    async def next_touch(self, timeout: float = 15):
        """Wait for the reference firmware's tap event, then acknowledge it."""
        if self.channel.capabilities.get("touch_demo") != 1:
            raise DeviceError("touch_demo_not_supported")
        async with asyncio.timeout(timeout):
            while True:
                async with self._operation:
                    if self.channel.events.empty():
                        await self.channel.send("heartbeat")
                    if not self.channel.events.empty():
                        return await self._acknowledge_touch()
                await asyncio.sleep(0.2)

    async def pending_touch(self):
        """Acknowledge one already received touch without waiting for a new one."""
        if self.channel.capabilities.get("touch_demo") != 1:
            return None
        async with self._operation:
            if self.channel.events.empty():
                return None
            return await self._acknowledge_touch()

    async def _acknowledge_touch(self):
        event = self.channel.events.get_nowait()
        try:
            await self.channel.send("touch_ack", boot=event["boot"],
                                    operation=event["operation"], status="accepted")
        except CommandRejected as error:
            # Older firmware rejects a valid event's ACK after its tap slot expires.
            if error.code != "unsupported_command":
                raise
        return event

    async def upload_file(self, path, source, *, expected_sha256=None, resume=None):
        """Transfer one file to the board's card, reporting a verified write."""
        if self.channel.capabilities.get("card_transfer") != 1:
            raise DeviceError("card_transfer_not_supported")
        if self.channel.capabilities.get("card_usb") == 1:
            async with self._operation:
                return await upload_usb(self.channel, path, source, expected_sha256=expected_sha256)
        background = self.channel.capabilities.get("card_fetch_async") == 1
        lock = self._upload if background else self._operation
        async with lock:
            return await upload_file(self.channel, self.link.peer_address, self.config.key, path, source,
                                     expected_sha256=expected_sha256, background=background,
                                     resume=resume if self.channel.capabilities.get("card_fetch_receipt") == 1 else None)

    async def delete_resource(self, path):
        if self.channel.capabilities.get("card_delete") != 1:
            raise DeviceError("card_delete_not_supported")
        validate_path(path)
        directory, name = path.split("/", 1)
        if (path.endswith(".") or "./" in path or
                (directory == "roles" and not valid_identity(name)) or
                (directory == "photos" and not re.fullmatch(r"p-[0-9a-f]{16}", name))):
            raise ValueError("请指定完整角色、相册照片，或 desk 下的单个文件。")
        async with self._upload, self._operation:
            return await self.channel.send("card_delete", path=path, timeout=30)

    async def prepare_role(self, identity, version, actions, finish_action=None):
        if self.channel.capabilities.get("role_actions") != 1:
            raise DeviceError("role_actions_not_supported")
        async with self._operation:
            return await self.channel.send("role_prepare", id=identity, version=version, actions=actions,
                                           finish_action=finish_action or "")

    async def list_roles(self):
        return await self._list_role_values("role_list", "roles", 64)

    async def list_actions(self):
        return await self._list_role_values("role_action_list", "actions", MAX_ROLE_ACTIONS)

    async def _list_role_values(self, command, field, limit):
        if self.channel.capabilities.get("role_actions") != 1:
            raise DeviceError("role_actions_not_supported")
        found, offset = [], 0
        async with self._operation:
            while True:
                result = await self.channel.send(command, offset=offset)
                values, next_offset = result.get(field), result.get("next_offset")
                if (not isinstance(values, list) or len(values) > 6 or
                        any(not isinstance(name, str) or not name or not name.isascii() or
                            len(name) > MAX_ROLE_ID_LENGTH for name in values) or
                        type(next_offset) is not int):
                    raise DeviceError("invalid_role_list")
                found.extend(values)
                if len(found) > limit or len(found) != len(set(found)):
                    raise DeviceError("invalid_role_list")
                if next_offset == -1:
                    return found
                if next_offset != offset + len(values) or not values or next_offset > limit:
                    raise DeviceError("invalid_role_list")
                offset = next_offset

    async def select_role(self, identity):
        if self.channel.capabilities.get("role_actions") != 1:
            raise DeviceError("role_actions_not_supported")
        async with self._operation:
            return await self.channel.send("role_select", id=identity)

    async def role_preview(self, mode="status", identity=None):
        if self.channel.capabilities.get("role_preview") != 1:
            raise DeviceError("role_preview_not_supported")
        if mode == "start":
            if not isinstance(identity, str) or not valid_identity(identity):
                raise ValueError("请选择已安装角色的标识。")
        elif mode not in ("status", "cancel", "use") or identity is not None:
            raise ValueError("只有开始试播时需要填写角色标识。")
        fields = {"id": identity} if identity is not None else {}
        async with self._operation:
            return await self.channel.send("role_preview", mode=mode, **fields)

    async def play_role(self, action, duration_ms):
        if self.channel.capabilities.get("role_actions") != 1:
            raise DeviceError("role_actions_not_supported")
        async with self._operation:
            return await self.channel.send("role_play", action=action, duration_ms=duration_ms)

    async def role_action_status(self, operation, boot):
        if self.channel.capabilities.get("role_action_receipt") != 1:
            raise DeviceError("role_action_receipt_not_supported")
        async with self._operation:
            return await self.channel.send("role_action_status", operation=operation, action_boot=boot)

    async def set_role_base(self, action):
        if self.channel.capabilities.get("role_base") != 1:
            raise DeviceError("role_base_not_supported")
        async with self._operation:
            return await self.channel.send("role_base", action=action)

    async def show_image(self, source):
        if self.channel.capabilities.get("image_display") != 1:
            raise DeviceError("image_display_not_supported")
        source = Path(source).expanduser()
        if not source.is_absolute() or not source.is_file():
            raise ValueError("请选择本机图片的绝对路径。")
        pixels = await asyncio.to_thread(prepare_image, source)
        path = "desk/images/" + hashlib.sha256(pixels).hexdigest()[:20] + ".565"
        with tempfile.TemporaryDirectory(prefix="deskpet-image-") as directory:
            prepared = Path(directory) / "image.565"
            prepared.write_bytes(pixels)
            await self.upload_file(path, prepared)
            async with self._operation:
                response = await self.channel.send("image_show", path=path)
        return {"status": response.get("image_status", "unknown"), "path": path}

    async def clear_image(self):
        if self.channel.capabilities.get("image_display") != 1:
            raise DeviceError("image_display_not_supported")
        async with self._operation:
            response = await self.channel.send("image_clear")
        return {"status": response.get("image_status", "unknown")}

    async def set_brightness(self, level):
        if type(level) is not int or not 0 <= level <= 100:
            raise ValueError("亮度须为 0 至 100 的整数。")
        if self.channel.capabilities.get("brightness_control") != 1:
            raise DeviceError("brightness_control_not_supported")
        async with self._operation:
            response = await self.channel.send("brightness_set", level=level)
        if response.get("brightness") != level:
            raise DeviceError("brightness_not_confirmed")
        return level

    async def calibrate_touch(self, cancel=False):
        if self.channel.capabilities.get("touch_coordinates") != 1:
            raise DeviceError("touch_coordinates_not_supported")
        async with self._operation:
            response = await self.channel.send("touch_calibrate_cancel" if cancel else "touch_calibrate")
        return {"status": response.get("touch_calibration", "unknown")}

    async def set_notice_count(self, count):
        if self.channel.capabilities.get("notices") != 1:
            return
        async with self._operation:
            await self.channel.send("notice_count", count=min(65535, max(0, count)))

    async def show_notice_detail(self, identity):
        if self.channel.capabilities.get("notices") != 1:
            return
        async with self._operation:
            await self.channel.send("notice_detail", notification_id=identity)

    async def pending_notice_reads(self):
        async with self._operation:
            reply = await self.channel.send("notice_pending")
        ids = reply.get("ids")
        if (not isinstance(ids, list) or len(ids) > 10 or
                any(type(identity) is not int or not 0 < identity <= 0xFFFFFFFF for identity in ids)):
            raise DeviceError("invalid_notice_reads")
        return ids

    async def acknowledge_notice_reads(self, ids):
        async with self._operation:
            await self.channel.send("notice_ack", ids=ids)

    async def message_view(self, mode="status", identity=None):
        if self.channel.capabilities.get("message_view") != 1:
            raise DeviceError("message_view_not_supported")
        if mode in ("open", "mark_seen"):
            if type(identity) is not int or not 0 < identity <= 0xFFFFFFFF:
                raise ValueError("请提供小屏消息列表中的通知编号。")
        elif mode not in ("status", "list", "previous", "next") or identity is not None:
            raise ValueError("只有打开通知或标记已读时需要填写编号。")
        fields = {"id": identity} if identity is not None else {}
        async with self._operation:
            return await self.channel.send("message_view", mode=mode, **fields)

    async def sync_messages(self, records):
        records = list(records)
        rows = [{"id": item["id"], "created": int(item["created_at"]), "seen": item["seen"],
                 "title": _notice_title(item["title"]).hex()} for item in records]
        async with self._operation:
            await self.channel.send("notice_cache_begin", count=len(rows))
            for record, row in zip(records, rows):
                await self.channel.send("notice_cache_row", **row)
                if self.channel.capabilities.get("message_metadata") == 1:
                    await self.channel.send("notice_cache_info", id=record["id"], title=record["title"],
                                            source=record["source"], category=record["category"])
            await self.channel.send("notice_cache_commit")

    async def show_text(self, text: str):
        """Draw a short test message, returning the actual board receipt."""
        bitmap = _bitmap(text)
        if self.channel.capabilities.get("text_present") != 1:
            raise DeviceError("text_not_supported")
        async with self._operation:
            state = await self.channel.send("heartbeat")
            if type(state.get("pet_page")) is not int or type(state.get("view_token")) is not int:
                raise DeviceError("设备未提供有效的文字显示状态。")
            if state["pet_page"] != 0:
                raise TextNotVisible("请在设备上打开桌宠首页。")
            generation = state.get("text_generation", 0) + 1
            answer = await self.channel.send(
                "text_begin", generation=generation, view_token=state["view_token"],
                region="toast", bytes=len(bitmap), crc32=zlib.crc32(bitmap),
                display_ms=4000, ttl_ms=3000,
            )
            if answer.get("text_status") != "receiving":
                if answer.get("text_reason") == "view_changed":
                    raise TextNotVisible("设备画面已切换，提醒将在画面可用时重试。")
                raise DeviceError("设备当前无法接收测试文字。")
            for index, offset in enumerate(range(0, len(bitmap), 192)):
                answer = await self.channel.send("text_chunk", generation=generation, index=index,
                                                 data=bitmap[offset:offset + 192].hex())
                if answer.get("text_status") != "receiving":
                    raise DeviceError("测试文字传输中断。")
            receipt = await self.channel.send("text_commit", generation=generation)
            if receipt.get("text_status") != "ready":
                return {"generation": generation, "status": receipt.get("text_status", "unknown")}
            for _ in range(8):
                await asyncio.sleep(0.1)
                state = await self.channel.send("heartbeat")
                status = state.get("text_status")
                if state.get("text_generation") == generation and status in ("shown", "finished", "cancelled", "expired"):
                    return {"generation": generation, "status": status}
            return {"generation": generation, "status": "display_unconfirmed"}


def _text_font(size):
    for path in ("/System/Library/Fonts/STHeiti Medium.ttc",
                 "/System/Library/Fonts/PingFang.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _notice_title(text):
    image = Image.new("1", (192, 16))
    draw = ImageDraw.Draw(image)
    font = _text_font(14)
    title = " ".join(text.split()) or "Codex"
    if draw.textlength(title, font=font) > image.width:
        title = title.rstrip()
        while draw.textlength(title + "...", font=font) > image.width:
            title = title[:-1]
        title += "..."
    _, top, _, bottom = draw.textbbox((0, 0), title, font=font)
    draw.text((0, (image.height - (bottom - top)) // 2 - top), title, fill=1, font=font)
    return image.tobytes()


def _bitmap(text):
    if (not isinstance(text, str) or not text.strip() or len(text) > 100 or text.count("\n") > 1 or
            any(character != "\n" and not character.isprintable() for character in text)):
        raise ValueError("请输入不超过两行的可打印文字。")
    image = Image.new("1", (288, 48))
    draw = ImageDraw.Draw(image)
    font = _text_font(16)
    missing = font.getmask("\U0010ffff")
    missing_glyph = (missing.size, bytes(missing))
    for character in set(text) - {"\n", " "}:
        glyph = font.getmask(character)
        if (glyph.size, bytes(glyph)) == missing_glyph:
            raise ValueError("电脑字体不支持这段文字中的部分字符，请换用受支持的字符。")
    lines = [""]
    for character in text:
        if character == "\n":
            lines.append("")
        elif draw.textlength(lines[-1] + character, font=font) > 272:
            lines.append(character)
        else:
            lines[-1] += character
        if len(lines) > 2:
            raise ValueError("文字显示不下，请缩短到两行以内。")
    for index, line in enumerate(lines):
        draw.text((8, 14 if len(lines) == 1 else 4 + index * 21), line, fill=1, font=font)
    return image.tobytes()
