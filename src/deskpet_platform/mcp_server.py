"""Local stdio MCP. Credentials are loaded from private disk storage."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, replace
import errno
import hashlib
from importlib.resources import files
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
import tempfile
import time
import sqlite3
from typing import Any, Literal, get_args
import webbrowser

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from .analytics_store import AnalyticsStore
from .analytics_sync import AnalyticsSync
from .adaptive_speed.config import Config as SpeedConfig
from .adaptive_speed.history import Frame, History
from .adaptive_speed.runtime import AdaptiveRuntime
from .agent_config import AgentConfigUnconfirmed
from .agent_speed_probe import AgentSpeedProbe, find_installed_probe
from .agent_tool_callback import fit_callback_result
from .card_upload import MAX_FILE_BYTES, MUSIC_DIRECTORIES, validate_music_path, validate_path
from .channel import AckTimeout, CommandRejected, DeviceError
from .device_lock import DeviceBusyError
from .client import (
    CAPTURE_FAILURE_DETAILS, DASHBOARD_INTERVALS, DASHBOARD_METRICS, DASHBOARD_ROTATION,
    SCREEN_PAGES, Client, _appearance_theme, _appearance_wallpaper,
)
from .codex_hook import configure_codex_hooks, configure_hooks, hook_command
from .codex_quota import fetch_quota
from .desktop_launch import launch_notification, supports_desktop_launch
from .computer_metrics import ComputerMetrics, DisplayPreferences
from .device_registry import DeviceRegistry
from .device_sessions import DeviceSessions
from .image_display import prepare_photo, prepare_photo_with_fullscreen, prepare_jpeg_photo
from .notifications import NotificationStore, codex_home, deliver_next, notification_text, request_worker_yield
from .codex_feedback import QuestionReconciler
from .notification_cache import NotificationCache
from .notification_view import NotificationView
from .pair_portal import PairPortal
from .pairing import DeviceConfig, valid_device_uuid
from .quota_capture import QuotaStore
from .quota_projection import ANTIGRAVITY_WINDOWS, SOURCES, project_quota
from .role_example import create_role_example
from .reference_collector import ReferenceCollector
from .role_requirements import inspect_role_requirements
from .role_sources import list_role_sources, select_role_source
from .role_staging import prepare_role_source
from .speed_burst import SpeedBurst
from .role_install import valid_identity
from .token_metrics import TokenMetrics
from .usb_setup import probe_usb
from .usb_link import UsbLink, UsbPort, find_usb_devices, read_usb_identity, reset_native_usb
from .weather import fetch_relay_weather, fetch_weather
from .speech import SpeechServiceError, SpeechTooLong, split_speech, synthesize_speech, synthesize_speech_mp3, validate_text
from .transcription import (
    DashScopeTranscription,
    LocalTranscription,
    TranscriptionError,
    configured_service,
    transcribe_dashscope_pcm,
    transcribe_local_pcm,
    transcribe_pcm,
)
from .voice_agent import AgentError, ChatBackend, configured_chat_backend
from .voice_conversation import create_device_conversation
from .voice_permissions import VOICE_TOOLS, VoicePermissions
from .board_readouts import BOARD_READOUTS
from .voice_tools import build_voice_tools, voice_tool_requires_confirmation, voice_tool_supported
from .wifi_link import NO_DEVICE_REPLY_HELP, probe_board

Manual = Literal["operations", "development", "roles"]
LINK_ERRORS = (OSError, RuntimeError, ValueError, TimeoutError)
WIFI_FALLBACK_TIMEOUT = 5
STATUS_WIFI_RECOVERY_TIMEOUT = 8
MAX_CODEX_ALLOWANCE_AGE = 600
NOTIFICATION_SETUP = {
    "codex": ("Codex", "desk_enable_codex_notifications", {}),
    "claude_code": ("Claude Code", "desk_set_claude_notifications", {"enabled": True}),
    "antigravity": ("Antigravity", "desk_set_antigravity_notifications", {"enabled": True}),
    "opencode": ("OpenCode", "desk_set_opencode_notifications", {"enabled": True}),
    "workbuddy": ("WorkBuddy", "desk_set_workbuddy_notifications", {"enabled": True, "edition": "workbuddy"}),
    "workbuddy_ai": ("WorkBuddy AI", "desk_set_workbuddy_notifications", {"enabled": True, "edition": "workbuddy_ai"}),
}


def require_notification_hooks(enabled, platform, hook=None):
    if enabled and platform == "nt":
        if hook not in ("claude-hook", "codex-hook"):
            raise ValueError("Windows 的此 Agent 提醒接入尚未验证；未修改 Agent 设置。")
        if os.name == "nt" and not shutil.which("powershell.exe"):
            raise ValueError("未找到 Windows PowerShell；未修改 Agent 设置。")


class CardFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    path: str


class _CaptureVoiceBackend:
    def __init__(self, backend, require_active):
        self.backend = backend
        self.require_active = require_active

    @property
    def supports_first_phrase(self):
        return isinstance(self.backend, ChatBackend) and self.backend.supports_first_phrase

    async def complete(self, messages, tools, *, on_first_phrase=None):
        await self.require_active()
        options = {'on_first_phrase': on_first_phrase} if on_first_phrase is not None else {}
        message = await self.backend.complete(messages, tools, **options)
        # The cancellation poll can be asleep when the model responds.
        await self.require_active()
        return message


class _OnboardToolReader:
    """Give the selected read-only tools access to the exact callback connection."""

    def __init__(self, client):
        self.client = client

    async def get_status(self):
        return status_view(await self.client.status(), self.client.channel.capabilities)

    async def list_actions(self):
        return {'status': 'ok', 'actions': await self.client.list_actions()}


def read_manual(topic: Manual) -> str:
    if topic not in get_args(Manual):
        raise ValueError("unknown_manual")
    return files("deskpet_platform").joinpath("manuals", topic + ".md").read_text(encoding="utf-8")


class BoardSession:
    VOICE_HISTORY_IDLE_SECONDS = 300

    def __init__(self, path, *, background=False, client_factory=Client, token_root=None, usb_port=None,
                 token_collector=None, token_probe=None, probe_collector=None, no_token_probe=False,
                 notification_source=None, quota_store=None, analytics_store=None):
        if (token_collector is not None and (token_probe is not None or probe_collector is not None) or
                token_probe is not None and probe_collector is not None):
            raise ValueError("请只选择一种 Token 速度采集器。")
        self.path = path
        self.background = background
        self.client_factory = client_factory
        self.identity_registry = None
        self.usb_port = UsbPort.register(usb_port, allow_missing=True) if usb_port is not None else None
        self.client = None
        self.device_info = None
        self.session_open_epoch = None
        self.next_wifi_probe = 0.0
        self.snapshot = {"connection": "starting"}
        self.lock = asyncio.Lock()
        self.speech_lock = asyncio.Lock()
        self.screen_speech_task = None
        self.capture_task = None
        self.capture_service = configured_service(os.environ)
        self.voice_backend = configured_chat_backend(os.environ)
        self.voice_conversation = None
        self.voice_idle_deadline = None
        self.voice_session_id = None
        self.voice_permissions = VoicePermissions(path)
        self.voice_policy_sent = None
        self.agent_tool_watermark = None
        self.voice_tool_snapshot = frozenset()
        self.voice_active_ticket = None
        self.voice_active_client = None
        self.storage_lock = asyncio.Lock()
        self.loop = None
        self.touches = asyncio.Queue(maxsize=32)
        self.dropped_touches = 0
        self.notifications = NotificationStore(Path(path).parent / "notifications.sqlite3", source=notification_source)
        self.codex_questions = QuestionReconciler(notification_source or self.notifications)
        self.codex_questions_due = 0.0
        self.notification_view = NotificationView(self.notifications)
        self.notification_cache = NotificationCache(self.notifications)
        self.next_notification_attempt = 0.0
        self.last_notice_count = None
        self.attention_client = None
        self.attention_count = None
        self.attention_source = None
        self.attention_due = 0.0
        self.last_message_cache = None
        self.message_cache_due = 0
        self.weather_path = Path(path).parent / "weather-location.json"
        self.weather_lock = asyncio.Lock()
        self.weather_due = 0.0
        codex_data = codex_home()
        self.token_metrics = TokenMetrics(token_root if token_root is not None else codex_data / "sessions")
        self.reference_collector = ReferenceCollector(token_collector) if token_collector is not None else None
        self.probe_collector = probe_collector if probe_collector is not None else (
            AgentSpeedProbe(token_probe) if token_probe is not None else None)
        self.owns_probe_collector = token_probe is not None
        self.no_token_probe = no_token_probe
        self.token_snapshot = None
        self.token_published_at = 0.0
        self.last_link_loss = None
        self.link_loss_started = None
        self.token_history_path = Path(path).parent / "token-speed.sqlite3"
        self.codex_allowance = {}
        self.codex_live_allowance = {}
        self.codex_live_observed_at = 0.0
        self.quota_store = quota_store if quota_store is not None else QuotaStore(Path(path).parent / "agent-quota.sqlite3")
        self.analytics = AnalyticsSync(analytics_store or AnalyticsStore(self.quota_store.path.with_name("analytics.sqlite3")),
                                       self.notifications, device=Path(path).resolve())
        self.computer_display = DisplayPreferences(Path(path).parent / "computer-display.json")
        self.computer_metrics = ComputerMetrics()
        self.computer_metrics_lock = asyncio.Lock()
        self.computer_metrics_changed = asyncio.Event()
        self.computer_preference_client = None

    def _detach_voice_conversation(self):
        conversation, self.voice_conversation = self.voice_conversation, None
        self.voice_idle_deadline = None
        self.voice_tool_snapshot = frozenset()
        return conversation

    async def drop(self):
        # Detach before awaiting cleanup so old work cannot target a replacement connection.
        client, self.client = self.client, None
        if self.capture_task and not self.capture_task.done():self.capture_task.cancel()
        self.capture_task=None
        conversation = self._detach_voice_conversation()
        self.voice_session_id = None
        self.voice_policy_sent = None
        self.voice_active_ticket=None
        self.voice_active_client=None
        if self.screen_speech_task and not self.screen_speech_task.done():self.screen_speech_task.cancel()
        self.screen_speech_task=None
        self.token_snapshot = None
        self.weather_due = 0
        self.computer_preference_client = None
        self.attention_client = None
        self.session_open_epoch = None
        try:
            if conversation is not None:await conversation.end()
        finally:
            if client is not None:
                self.dropped_touches += getattr(client.channel, "dropped_events", 0)
                await client.close()

    async def _drop_failed_upload(self, client):
        async with self.lock:
            if self.client is client and not client.channel.online:
                await self.drop()
                self.snapshot = {"connection": "offline"}

    def usb_client(self, config, port):
        return Client(config, link_factory=lambda *args, **fields: UsbLink(*args, port=port, **fields))

    async def set_usb_fallback(self, port, device_id):
        async with self.storage_lock, self.lock:
            if self.client is not None and not self.client.link.wireless:
                raise ValueError("小板正在通过 USB 控制，请在无线恢复后更新备用串口。")
            config = DeviceConfig.load(self.path)
            if config.device_id != device_id:
                raise DeviceError("device_identity_mismatch")
            registration = UsbPort.register(port)
            reading = asyncio.create_task(asyncio.to_thread(read_usb_identity, registration))
            try:
                observed = await asyncio.shield(reading)
            except asyncio.CancelledError:
                # Keep the session locked until the read-only serial probe closes.
                with suppress(OSError, ValueError):
                    await reading
                raise
            if (observed.identity, observed.device_id) != (config.identity, config.device_id):
                raise DeviceError("device_identity_mismatch")
            registration.verify()
            if DeviceConfig.load(self.path) != config:
                raise DeviceError("device_pairing_changed")
            self.usb_port = registration
        return {"port": registration.path, "device": config.identity, "device_id": device_id}

    async def connect(self, config):
        try:
            return await self.open_wifi(config)
        except (OSError, AckTimeout):
            if self.usb_port is None:
                raise
            registration = self.usb_port
            try:
                registration.verify()
            except OSError:
                if config.device_id is None:
                    raise
                registration = UsbPort.register(registration.path)
                observed = await asyncio.to_thread(read_usb_identity, registration)
                if (observed.identity, observed.device_id) != (config.identity, config.device_id):
                    raise DeviceError("device_identity_mismatch")
            board = await self.usb_client(config, registration).__aenter__()
            self.usb_port = registration
            return board

    async def open_wifi(self, config):
        async with asyncio.timeout(WIFI_FALLBACK_TIMEOUT if self.usb_port is not None else None):
            client = await self.client_factory(config).__aenter__()
        try:
            await self.sync_onboard_voice_permissions(client)
            return client
        except BaseException:
            await client.close()
            raise

    async def recover_wifi(self, client, heartbeat):
        if (client is None or client is not self.client or self.usb_port is None or
                client.channel.capabilities.get("wifi_probe") != 1 or self.storage_lock.locked() or
                not heartbeat.get("wifi_handoff_ready") or time.monotonic() < self.next_wifi_probe):
            return
        address = heartbeat.get("wifi_ip")
        if not address:
            return
        config = client.config
        self.next_wifi_probe = time.monotonic() + 5
        if not await probe_board(config.identity, config.key, address) or self.storage_lock.locked():
            return
        async with self.storage_lock, self.lock:
            if client is not self.client:
                return
            try:
                latest = await client.status()
            except LINK_ERRORS:
                return  # The regular heartbeat will handle the broken link.
            if not latest.get("wifi_handoff_ready") or latest.get("wifi_ip") != address:
                return
            self.next_wifi_probe = time.monotonic() + 30
            self.snapshot = {"connection": "connecting"}
            try:
                await self.drop()
                wifi_config = replace(config, address=address)
                self.client = await self.open_wifi(wifi_config)
            except LINK_ERRORS:
                try:
                    self.client = await self.usb_client(config, self.usb_port).__aenter__()
                except LINK_ERRORS:
                    return  # Retry through the regular connection path.
            else:
                self.next_wifi_probe = 0.0
                if address != config.address:
                    with suppress(OSError, ValueError):
                        if DeviceConfig.load(self.path) == config:
                            wifi_config.save(self.path)
            self.last_notice_count = self.last_message_cache = None

    def record_status(self, heartbeat):
        status = status_view(heartbeat, self.client.channel.capabilities)
        wireless = getattr(getattr(self.client, "link", None), "wireless", None)
        if type(wireless) is bool:
            status["transport"] = "wifi" if wireless else "usb"
        direction = getattr(getattr(self.client, "link", None), "connection_direction", None)
        if direction in ("direct", "callback"):
            status["wifi_connection_direction"] = direction
        if self.snapshot.get("speed_windows") != status.get("speed_windows"):
            self.token_snapshot = None
        if self.last_link_loss is not None:
            if "connection_recovery_ms" not in self.last_link_loss:
                self.last_link_loss["connection_recovery_ms"] = self._elapsed_since_link_loss()
            status["last_link_loss"] = self.last_link_loss.copy()
        self.snapshot = status

    def _elapsed_since_link_loss(self):
        return round((time.monotonic() - self.link_loss_started) * 1000)

    async def record_link_error(self, error):
        if self.client is not None and self.snapshot.get("connection") == "connected":
            if isinstance(error, AckTimeout):
                reason = "ack_timeout"
            elif isinstance(error, CommandRejected) and error.code == "handshake_required":
                reason = "handshake_required"
            elif isinstance(error, OSError) and str(error).startswith("wifi_disconnected"):
                reason = "wifi_stream_closed"
            elif isinstance(error, OSError) and str(error).startswith("usb_disconnected"):
                reason = "usb_stream_closed"
            else:
                reason = "link_error"
            self.link_loss_started = time.monotonic()
            had_speed = self.token_snapshot is not None and self.link_loss_started - self.token_published_at < 15
            self.last_link_loss = {"observed_at": time.time(), "reason": reason, "speed_was_active": had_speed}
        await self.drop()
        busy = isinstance(error, RuntimeError) and str(error).startswith("device_busy:")
        handoff = False
        if busy and not self.background:
            with suppress(OSError):
                handoff = request_worker_yield(self.path)
        cause = error.__cause__
        network_unreachable = (isinstance(error, OSError) and str(error) == 'wifi_discovery_unreachable'
                               and isinstance(cause, OSError) and cause.errno in
                               (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM))
        if busy:
            next_step = '已请求后台服务让出设备，请稍后重试；若持续占用，请关闭连接此设备的其他会话或程序。' if handoff else '设备正被其他程序使用，请关闭该程序后重试。'
        elif network_unreachable:
            next_step = '无法访问局域网，请检查网络连接、路由和系统的局域网权限。'
        elif isinstance(error, OSError) and str(error) == 'wifi_device_absent':
            next_step = NO_DEVICE_REPLY_HELP
        else:
            next_step = '检查供电、Wi-Fi 和配对，详见操作手册。'
        self.snapshot = {
            "connection": "busy" if busy else "offline",
            "next_step": next_step,
        }
        if network_unreachable:
            self.snapshot['reason'] = 'local_network_unreachable'
        if self.last_link_loss is not None:
            self.snapshot["last_link_loss"] = self.last_link_loss.copy()

    async def record_status_rejection(self, error):
        if error.code == "handshake_required":
            await self.record_link_error(error)
        elif self.client is None:
            self.snapshot = {"connection": "offline"}
        self.snapshot["device_error"] = error.code

    async def _read_status(self):
        previous = self.client
        try:
            return await previous.status()
        except (AckTimeout, OSError) as error:
            if isinstance(error, OSError) and str(error).partition(":")[0] != "wifi_disconnected":
                raise
            if (self.snapshot.get("connection") != "connected" or self.storage_lock.locked() or
                    getattr(getattr(previous, "link", None), "wireless", False) is not True):
                raise
            await self.record_link_error(error)

        config = DeviceConfig.load(self.path)
        if config != previous.config:
            raise DeviceError("device_pairing_changed")
        async with asyncio.timeout(STATUS_WIFI_RECOVERY_TIMEOUT):
            replacement = await self.open_wifi(config)
            try:
                if replacement.device_id != previous.device_id:
                    raise DeviceError("device_identity_mismatch")
                # Leave a new boot for the regular poll connection setup.
                if replacement.channel.boot != previous.channel.boot:
                    raise DeviceError("device_restarted")
                heartbeat = await replacement.status()
                if DeviceConfig.load(self.path) != config:
                    raise DeviceError("device_pairing_changed")
            except BaseException:
                await replacement.close()
                raise
        self.client = replacement
        self.last_notice_count = self.last_message_cache = None
        return heartbeat

    async def get_status(self):
        if self.client is None or self.storage_lock.locked():
            return self.snapshot.copy()
        async with self.lock:
            if self.client is None or self.storage_lock.locked():
                return self.snapshot.copy()
            notification_error = self.snapshot.get("notification_error")
            try:
                heartbeat = await self._read_status()
            except CommandRejected as error:
                await self.record_status_rejection(error)
            except LINK_ERRORS as error:
                await self.record_link_error(error)
            else:
                self.record_status(heartbeat)
                await self.attach_device_info()
                if notification_error is not None:
                    self.snapshot["notification_error"] = notification_error
            return self.snapshot.copy()

    async def attach_device_info(self):
        client = self.client
        if client.channel.capabilities.get("device_info") != 1:
            return
        if self.device_info is None or self.device_info[0] is not client:
            try:
                self.device_info = (client, await client.device_info())
            except LINK_ERRORS as error:
                self.snapshot["device_info_error"] = getattr(error, "code", None) or str(error).split(":", 1)[0]
                return
        self.snapshot["device_info"] = self.device_info[1]

    async def poll(self):
        sampled_client, heartbeat = None, {}
        while True:
            now = time.monotonic()
            if now >= self.codex_questions_due:
                self.codex_questions_due = now + 5
                try:
                    await asyncio.to_thread(self.codex_questions.run)
                except (OSError, sqlite3.Error, ValueError):
                    self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
            await self.recover_wifi(sampled_client, heartbeat)
            await self.refresh_onboard_tool_callback(self.client)
            async with self.lock:
                try:
                    if self.client is None:
                        config = DeviceConfig.load(self.path)
                        self.snapshot = {"connection": "connecting"}
                        self.client = await self.connect(config)
                        observed = getattr(self.client, "device_id", None)
                        if self.identity_registry is not None and config.device_id is None and valid_device_uuid(observed):
                            self.client.config = self.identity_registry.upgrade(self.path, config, observed)
                        self.last_notice_count = None
                        self.last_message_cache = None
                        if self.client.channel.capabilities.get("clock_sync") == 1:
                            await self.client.sync_clock(only_missing=True)
                    channel = self.client.channel
                    compact = (channel.capabilities.get("compact_status") == 1 and
                               getattr(getattr(self.client, "link", None), "wireless", False) is True)
                    heartbeat = await self.client.status(compact=True) if compact else await self.client.status()
                    if (channel.capabilities.get("agent_cache_first") == 1 and
                            channel.epoch != self.session_open_epoch and supports_desktop_launch()):
                        try:
                            await self.client.enable_session_open()
                        except CommandRejected as error:
                            if error.code not in ("card_busy", "role_busy"):
                                raise
                        else:
                            self.session_open_epoch = channel.epoch
                    sampled_client = self.client
                    if self.client.channel.capabilities.get('message_read_aloud')==1:
                        await self.refresh_read_request(self.client)
                    await self.sync_onboard_voice_permissions(self.client)
                    await self.refresh_capture(self.client)
                    await self.sync_computer_preferences(sampled_client, heartbeat)
                    self.record_status(heartbeat)
                    notification_sync_ok = True
                    try:
                        await asyncio.to_thread(self.notifications.sync)
                    except (OSError, sqlite3.Error, ValueError):
                        notification_sync_ok = False
                        self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                    if notification_sync_ok and self.client.channel.capabilities.get("attention_summary") == 1:
                        await self.refresh_attention_summary(self.client)
                    if notification_sync_ok and channel.capabilities.get("agent_analytics") == 1:
                        try:
                            await self.analytics.refresh_attention(self.client)
                        except (sqlite3.Error, ValueError):
                            self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                    if self.client.channel.capabilities.get("touch_demo") == 1:
                        while event := await self.client.pending_touch():
                            try:
                                await self.process_touch(event)
                            except (sqlite3.Error, ValueError):
                                self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                    if self.client.channel.capabilities.get("notices") == 1:
                        try:
                            if self.client.channel.capabilities.get("message_cache") == 1:
                                await self.refresh_messages()
                            card_summary = (self.client.channel.capabilities.get("message_cache") == 1 and
                                            self.client.channel.capabilities.get("message_card_summary") == 1)
                            count = None if card_summary else min(65535, self.notifications.unseen_count(for_device=True))
                        except (sqlite3.Error, ValueError):
                            self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                        else:
                            if count is not None and count != self.last_notice_count:
                                await self.client.set_notice_count(count)
                                self.last_notice_count = count
                            capabilities = self.client.channel.capabilities
                            if capabilities.get("message_cache") == 1 and capabilities.get("agent_home") != 1:
                                try:
                                    await asyncio.to_thread(self.notifications.summarize_legacy_backlog)
                                except (sqlite3.Error, ValueError):
                                    self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                    if self.client.channel.capabilities.get("agent_home") == 1:
                        try:
                            await self.refresh_agent_notifications(heartbeat)
                        except (sqlite3.Error, ValueError):
                            self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                    elif (self.client.channel.capabilities.get("text_present") == 1 and
                          heartbeat.get("pet_page") == 0 and time.monotonic() >= self.next_notification_attempt):
                        self.next_notification_attempt = time.monotonic() + 4
                        try:
                            await deliver_next(self.client, self.notifications, legacy_batch=channel.capabilities.get("message_cache") == 1)
                        except (sqlite3.Error, ValueError):
                            self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
                except FileNotFoundError as error:
                    if self.client is None and error.filename == os.fspath(self.path):
                        self.snapshot = {"connection": "not_paired", "next_step": "在本机运行 deskpet-platform pair。"}
                    else:
                        await self.record_link_error(error)
                except CommandRejected as error:
                    await self.record_status_rejection(error)
                except LINK_ERRORS as error:
                    await self.record_link_error(error)
            delay = 3
            if self.client is not None:
                delay = 1
                # Each batch already yields to status, touch and other commands.
                # Keep draining an in-flight TF commit without the idle poll delay.
                if (self.client.channel.capabilities.get("agent_cache_first") == 1
                        and self.notification_cache.transfer is not None):
                    delay = .1
            await asyncio.sleep(delay)

    async def refresh_attention_summary(self, client, *, now=None):
        if client.channel.capabilities.get("attention_summary") != 1:
            return
        now = time.monotonic() if now is None else now
        try:
            summary = await asyncio.to_thread(self.notifications.home_summary, for_device=True)
        except (OSError, sqlite3.Error, ValueError):
            self.snapshot["notification_error"] = "提醒记录不可用，请检查本机存储。"
            return
        count = min(65535, summary["response_count"])
        supports_source=client.channel.capabilities.get('attention_source')==1
        sources=summary['response_sources']
        source=0
        if count and len(sources)>1:
            source=6
        elif count and sources:
            source={'codex':1,'claude_code':2,'antigravity':3,'opencode':4,'workbuddy':5}.get(sources[0]['source'],0)
        current_source=source if supports_source else None
        if client is not self.attention_client or count != self.attention_count or current_source != self.attention_source or now >= self.attention_due:
            if supports_source:await client.set_attention_summary(count,source=source)
            else:await client.set_attention_summary(count)
            self.attention_client = client
            self.attention_count = count
            self.attention_source = current_source
            self.attention_due = now + 5

    async def refresh_messages(self):
        client = self.client
        card_summary = client.channel.capabilities.get("message_card_summary") == 1
        session_open = client.channel.capabilities.get("notice_session_open") == 1
        if card_summary:
            ids, clear, dismissals = await client.pending_notice_feedback()
            for item in dismissals:
                await asyncio.to_thread(self.notifications.dismiss_legacy_response, item["id"], item["created"])
        else:
            ids = await client.pending_notice_reads()
            dismissals = []
        if ids:
            await asyncio.to_thread(self.notifications.mark_seen_many, ids, grouped_completions=card_summary)
        acknowledged = ids + [item["id"] for item in dismissals]

        fields = {}
        stamp = None
        if card_summary:
            records, summary = await asyncio.to_thread(self.notifications.legacy_message_snapshot, *clear,
                                                       include_session_open=session_open and supports_desktop_launch())
            stamp = (summary, clear)
            fields = {"summary": summary, "clear": clear}
        else:
            records = await asyncio.to_thread(self.notifications.list, 10, for_device=True)
        if session_open:
            await self.refresh_message_session_open(client, records)
        if acknowledged:
            try:
                await client.acknowledge_notice_reads(acknowledged)
            except CommandRejected as error:
                if error.code != "low_battery_readonly":
                    raise
                return
        fingerprint = (stamp, [(item["id"], item["seen"], item["title"]) for item in records])
        if fingerprint != self.last_message_cache or (card_summary and time.monotonic() >= self.message_cache_due):
            responses = [item["id"] for item in records if item["category"] == "response"]
            await asyncio.to_thread(self.notifications.record_response_projection, "legacy", responses)
            try:
                await client.sync_messages(records, **fields)
            except CommandRejected as error:
                if error.code == "low_battery_readonly":
                    return
                if not card_summary or error.code not in ("notice_cache_busy", "notice_cache_commit_failed"):
                    raise
                # A local read/clear can invalidate a snapshot while it is being sent.
                return
            await asyncio.to_thread(self.notifications.record_response_projection, "legacy", responses, confirmed=True)
            self.last_message_cache = fingerprint
            self.message_cache_due = time.monotonic() + 8

    async def refresh_message_session_open(self, client, records):
        rows = [[item["id"], int(item["created_at"])] for item in records if item.get("open_session")]
        request = await client.pending_message_session_open(rows)
        if request is None:
            return
        accepted = False
        try:
            if [request["id"], request["created"]] in rows:
                args = await asyncio.to_thread(self.notifications.pending_session_launch_args,
                                               request["id"], request["created"])
                if args:
                    accepted = await asyncio.to_thread(launch_notification, args)
        except (OSError, sqlite3.Error, subprocess.SubprocessError):
            pass  # Return a failed launch to the detail screen.
        await client.finish_message_session_open(request["ticket"], accepted=accepted)

    async def refresh_agent_notifications(self, heartbeat):
        if self.client is not None:
            channel = self.client.channel
            cache_first = channel.capabilities.get("agent_cache_first") == 1
            if cache_first:
                await self.notification_cache.step(self.client)
            await self.notification_view.refresh(self.client, device_page=heartbeat.get("pet_page"),
                                                 displayed_version=heartbeat.get("displayed_version"),
                                                 role_ready=heartbeat.get("role_state") == "ready",
                                                 cached_records=self.notification_cache.committed_records(channel))
            if not cache_first:
                await self.notification_cache.step(self.client)

    async def message_view(self, mode, identity):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            capabilities = self.client.channel.capabilities
            if capabilities.get("message_view") != 1:
                if capabilities.get("agent_home") != 1 or capabilities.get("screen_views") != 1:
                    return {"status": "unsupported"}
                if mode != "list":
                    return {"status": "unsupported", "next_step": "用 list 打开小屏提醒列表，再在小屏上翻页或查看详情。"}
                if identity is not None:
                    raise ValueError("打开小屏提醒列表无需填写通知编号。")
                try:
                    reply = await self.client.set_view("messages")
                except CommandRejected as error:
                    if error.code != "cache_not_ready":
                        raise
                    return {"status": "cache_not_ready", "next_step": "请确认储存卡已插入，并等待后台下载提醒。"}
                if reply.get("screen_page") != SCREEN_PAGES.index("messages"):
                    raise DeviceError("message_list_not_confirmed")
                return {"status": "ok", "view": "list"}
            reply = await self.client.message_view(mode, identity)
            fields = ("view", "cached_count", "cached_unread", "page", "pages", "messages", "message")
            return {"status": "ok", **{key: reply[key] for key in fields if key in reply}}

    async def speak(self,text, *, read_ticket=None):
        text=validate_text(text)
        return await self._speak_text(text,read_ticket=read_ticket)

    async def speak_voice_reply(self, text, *, capture_ticket=None, continuation: asyncio.Future[str] | None = None):
        """Read a bounded assistant reply as one interruptible, serialized operation."""
        return await self._speak_text(
            self._normalize_voice_reply(text), capture_ticket=capture_ticket,
            expected_client=self.voice_active_client, continuation=continuation,
        )

    @staticmethod
    def _normalize_voice_reply(text):
        try:
            if not isinstance(text,str) or len(text.encode('utf-8'))>8192:
                raise ValueError
            if any((ord(char)<32 and char not in '\n\r\t') or ord(char)==127 for char in text):
                raise ValueError
            text=' '.join(text.split())
            if not text:raise ValueError
        except (ValueError,UnicodeError):
            raise ValueError('语音回复为空或格式无效。') from None
        return text

    async def _speak_text(self, text, *, read_ticket=None, capture_ticket=None, expected_client=None, continuation=None):
        speech_request = {}
        if read_ticket is not None:
            speech_request['read_ticket'] = read_ticket
        if capture_ticket is not None:
            speech_request['capture_ticket'] = capture_ticket

        async with self.speech_lock:
            async with self.lock:
                client = self.client
                if client is None or (expected_client is not None and client is not expected_client):
                    return {"status": "not_connected", "connection": self.snapshot}
                channel = client.channel
                use_mp3 = channel.capabilities.get("speech_mp3") == 1
                if not use_mp3 and channel.capabilities.get("speech_pcm") != 1:
                    return {"status": "unsupported"}
                if read_ticket is not None and channel.capabilities.get("read_speech_ticket") != 1:
                    return {"status": "read_cancel_not_supported"}
                epoch = getattr(channel, "epoch", None)

            def connection_matches():
                return (self.client is client and getattr(channel, "online", True)
                        and getattr(channel, "epoch", None) == epoch)

            synthesize = synthesize_speech_mp3 if use_mp3 else synthesize_speech
            play = client.play_speech_mp3 if use_mp3 else client.play_speech_pcm
            parts = deque(part for part in split_speech(text) if part.strip())
            prefetch = use_mp3 or continuation is not None

            async def next_clip():
                nonlocal continuation
                if not parts and continuation is not None:
                    remaining = await asyncio.shield(continuation)
                    continuation = None
                    if remaining:
                        parts.extend(part for part in split_speech(self._normalize_voice_reply(remaining)) if part.strip())
                while parts and connection_matches():
                    part = parts.popleft()
                    try:
                        return await synthesize(part)
                    except SpeechTooLong:
                        if len(part) < 2:
                            raise
                        middle = len(part) // 2
                        parts.extendleft(half for half in (part[middle:], part[:middle]) if half.strip())
                return None

            duration = 0
            pending = None
            try:
                while parts or continuation is not None or pending is not None:
                    if not connection_matches():
                        return {"status": "not_connected", "connection": self.snapshot}
                    request, pending = pending, None
                    audio = await (request if request is not None else next_clip())
                    if not connection_matches():
                        return {"status": "not_connected", "connection": self.snapshot}
                    if audio is None:
                        break
                    if prefetch and (parts or continuation is not None):
                        pending = asyncio.create_task(next_clip())
                    result = await play(audio, **speech_request)
                    duration += result['duration_ms']
                return {"status": "finished", "duration_ms": duration}
            finally:
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def read_notification(self,identity, *, read_ticket=None):
        item=await asyncio.to_thread(self.notifications.get,identity)
        if item is None:return {"status":"not_found"}
        return await self.speak(item['title'],read_ticket=read_ticket) if read_ticket is not None else await self.speak(item['title'])

    async def refresh_capture(self, client):
        capabilities = client.channel.capabilities
        if (client is not self.client or not self.capture_service or capabilities.get('capture_pull') != 1 or
                capabilities.get('transcript_display') != 1 or capabilities.get('capture_transcript_ticket') != 1 or
                capabilities.get('capture_cancel_poll') != 1):
            return
        if self.capture_task and not self.capture_task.done():
            return
        voice_ready=(self.voice_backend is not None and capabilities.get('voice_names')==1 and capabilities.get('speech_pcm')==1)
        metadata = await client.poll_capture(voice_ready=voice_ready) if capabilities.get('capture_mode')==1 else await client.poll_capture()
        if client is not self.client or (self.capture_task and not self.capture_task.done()):
            return
        if not isinstance(metadata, dict):
            return
        session_id = self.voice_session_id
        if capabilities.get('capture_voice_session') == 1:
            session_id = metadata.get('voice_session')
            if type(session_id) is not int or not 0 <= session_id <= 0xffffffff:
                return
        history_expired = self.voice_idle_deadline is not None and time.monotonic() >= self.voice_idle_deadline
        if session_id != self.voice_session_id or history_expired:
            conversation = self._detach_voice_conversation()
            self.voice_session_id = session_id
            if conversation is not None:
                await conversation.end()
                if (client is not self.client or self.voice_session_id != session_id or
                        (self.capture_task and not self.capture_task.done())):
                    return
        ticket = metadata.get('ticket')
        if type(ticket) is not int or not 0 < ticket <= 0xffffffff:
            return
        self.capture_task = asyncio.create_task(self._screen_capture(client, metadata))

    def _advance_agent_tool_watermark(self, client, call_id):
        scope = (client.config.identity, client.device_id, client.channel.boot)
        previous = self.agent_tool_watermark
        if previous is not None and previous[0] == scope:
            distance = (call_id - previous[1]) & 0xffffffff
            if not 0 < distance < 0x80000000:
                return False
        self.agent_tool_watermark = (scope, call_id)
        return True

    def _agent_tool_bound(self, client, epoch, boot):
        return (self.client is client and client.channel.online and client.channel.epoch == epoch and
                client.channel.boot == boot)

    async def refresh_onboard_tool_callback(self, client):
        if client is None or getattr(getattr(client, 'link', None), 'wireless', False) is not True:
            return
        channel = client.channel
        if any(channel.capabilities.get(name) != 1 for name in ('cap_ext', 'assistant_onboard', 'agent_tool_callback')):
            return
        epoch, boot = channel.epoch, channel.boot
        try:
            async with self.lock:
                if not self._agent_tool_bound(client, epoch, boot):
                    return
                if await self.sync_onboard_voice_permissions(client) != 'applied':
                    return
                call = await client.poll_agent_tool()
                if call is None or not self._advance_agent_tool_watermark(client, call['id']):
                    return

            # Keep the lock order used by Base tools. The read-only handlers are
            # bound to this client and cannot reconnect through BoardSession.
            async with self.voice_permissions.lock(), self.lock:
                if (not self._agent_tool_bound(client, epoch, boot) or
                        not self._matches_voice_pairing(client, DeviceConfig.load(self.path))):
                    return
                name = call['name']
                success, result = False, {'status': 'not_authorized'}
                if name in self.voice_permissions.enabled():
                    result = {'status': 'unsupported'}
                    if voice_tool_supported(name, channel.capabilities):
                        tool, = build_voice_tools(_OnboardToolReader(client), {name})
                        try:
                            outcome = await tool.invoke()
                        except CommandRejected:
                            result = {'status': 'unavailable'}
                        else:
                            success, result = fit_callback_result(name, outcome)
                if (not self._agent_tool_bound(client, epoch, boot) or
                        not self._matches_voice_pairing(client, DeviceConfig.load(self.path))):
                    return
                if name not in self.voice_permissions.enabled():
                    success, result = False, {'status': 'not_authorized'}
                await client.send_agent_tool_result(call['ticket'], call['id'], success, result)
        except CommandRejected:
            # A cancelled or expired board turn may reject its result. No callback is retried.
            return
        except LINK_ERRORS as error:
            async with self.lock:
                if self.client is client and not channel.online:
                    await self.record_link_error(error)

    async def _require_active_capture(self, client, ticket):
        if self.client is not client:
            raise DeviceError('connection_changed')
        # Channel owns the ACK deadline. Cancelling the check early invalidates the connection.
        active = await client.capture_active(ticket)
        if not active or self.client is not client:
            raise DeviceError('capture_cancelled')

    async def _screen_capture(self, client, metadata):
        if client is not self.client:
            return
        ticket = metadata['ticket']
        capabilities = client.channel.capabilities
        received = False
        success = False
        failure = 'recognition'
        detail = None
        mode=metadata.get('mode','transcript')
        display_reply = mode == 'voice' and capabilities.get('capture_reply_text') == 1
        mode_valid=(mode in ('transcript','voice') and
                    (('mode' not in metadata and client.channel.capabilities.get('capture_mode')!=1) or
                     ('mode' in metadata and client.channel.capabilities.get('capture_mode')==1)))
        if mode == 'voice' and capabilities.get('capture_voice_session') == 1:
            mode_valid = mode_valid and self.voice_session_id == metadata.get('voice_session') and bool(self.voice_session_id)
        try:
            if not mode_valid or (mode=='voice' and self.voice_backend is None):
                if mode_valid and mode=='voice':failure='model'
                await client.discard_capture(ticket);received=True;return
            pcm = await client.receive_capture(metadata)
            received = True
            if self.client is not client:
                return
            service = self.capture_service
            async def process():
                nonlocal failure
                if isinstance(service, LocalTranscription):
                    text=await transcribe_local_pcm(pcm,service)
                elif isinstance(service, DashScopeTranscription):
                    text=await transcribe_dashscope_pcm(pcm,service)
                else:
                    key, model = service
                    text=await transcribe_pcm(pcm,api_key=key,model=model)
                if not isinstance(text, str):
                    raise TranscriptionError('invalid_response')
                if not text.strip():
                    raise TranscriptionError('no_speech')
                await self._require_active_capture(client, ticket)
                if mode!='voice':
                    await client.queue_transcript(text,capture_ticket=ticket);return
                if display_reply:
                    await client.queue_transcript(text, capture_ticket=ticket)
                failure='model';await client.capture_progress(ticket,'thinking')
                if self.client is not client:raise DeviceError('connection_changed')
                enabled = frozenset(name for name in self.voice_permissions.enabled()
                                    if voice_tool_supported(name, capabilities))
                if self.voice_conversation is not None and enabled!=self.voice_tool_snapshot:
                    conversation=self._detach_voice_conversation()
                    await conversation.end()
                    if self.client is not client:raise DeviceError('connection_changed')
                if self.voice_conversation is None:
                    def authorize(name):
                        return self.client is client and name in self.voice_permissions.enabled()

                    async def require_active_turn():
                        # This conversation is reused across several capture tickets.
                        active_ticket = self.voice_active_ticket
                        if self.voice_active_client is not client or active_ticket is None:
                            raise DeviceError('capture_cancelled')
                        await self._require_active_capture(client, active_ticket)

                    @asynccontextmanager
                    async def permission_lock():
                        async with self.voice_permissions.lock():
                            await require_active_turn()
                            yield

                    backend = _CaptureVoiceBackend(self.voice_backend, require_active_turn)
                    conversation=await create_device_conversation(self,backend,enabled_tools=enabled,authorize=authorize,
                        permission_lock=permission_lock,voice_role_action=self.voice_role_action)
                    if self.client is not client:
                        await conversation.end()
                        raise DeviceError('connection_changed')
                    self.voice_conversation=conversation
                    self.voice_tool_snapshot=enabled
                async def speak_reply(reply_text):
                    nonlocal failure
                    failure = 'playback'
                    if display_reply:
                        await client.queue_transcript(reply_text, capture_ticket=ticket, kind='reply')
                    await client.capture_progress(ticket, 'speaking')
                    if self.client is not client:raise DeviceError('connection_changed')
                    if capabilities.get('capture_speech_ticket')==1:
                        return await self.speak_voice_reply(reply_text,capture_ticket=ticket)
                    return await self.speak_voice_reply(reply_text)

                async def speak_stream(first_phrase, *, continuation):
                    nonlocal failure
                    failure = 'playback'
                    # The model stream can receive text between cancellation polls.
                    await self._require_active_capture(client, ticket)
                    await client.capture_progress(ticket, 'speaking')
                    if self.client is not client:
                        raise DeviceError('connection_changed')
                    return await self.speak_voice_reply(first_phrase, capture_ticket=ticket,
                                                        continuation=continuation)

                async def show_streamed_reply(full_reply):
                    if display_reply:
                        await client.queue_transcript(full_reply, capture_ticket=ticket, kind='reply')

                self.voice_active_ticket=ticket;self.voice_active_client=client
                try:
                    stream = speak_stream if capabilities.get('capture_speech_ticket') == 1 else None
                    reply = await self.voice_conversation.turn(text, speak_reply=speak_reply, speak_stream=stream,
                                                               show_streamed_reply=show_streamed_reply)
                finally:
                    if self.voice_active_client is client and self.voice_active_ticket==ticket:
                        self.voice_active_ticket=None;self.voice_active_client=None
                if reply.agent.confirmation is not None or not reply.playback or reply.playback.get('status')!='finished':
                    raise RuntimeError('voice_reply_incomplete')
                if not display_reply:
                    failure = 'recognition'
                    await self._require_active_capture(client, ticket)
                    await client.queue_transcript(text, capture_ticket=ticket)
            task=asyncio.create_task(process())
            try:
                while not task.done():
                    done,_=await asyncio.wait({task},timeout=.5)
                    if done:break
                    await self._require_active_capture(client, ticket)
                await task
                success = True
            finally:
                if not task.done():task.cancel()
                with suppress(asyncio.CancelledError,Exception):await task
        except asyncio.CancelledError:
            raise
        except TranscriptionError as exc:
            code = str(exc)
            if code == 'no_speech' and capabilities.get('capture_no_speech') == 1:
                failure='no_speech'
            else:
                detail = 'service_unreachable' if code in ('network_error', 'service_error') else code
        except AgentError as exc:
            failure = 'model'
            detail = {
                'model_authentication_failed': 'invalid_token',
                'model_rate_limited': 'rate_limited',
                'model_invalid_response': 'invalid_response',
                'model_invalid_tool_calls': 'invalid_response',
                'model_timeout': 'service_timeout',
                'model_connection_failed': 'service_unreachable',
                'model_endpoint_or_model_unavailable': 'service_unreachable',
                'model_service_failed': 'service_unreachable',
            }.get(exc.code)
        except SpeechServiceError as exc:
            detail = exc.code
        except Exception:
            pass  # Never log credentials, recorded audio or remote response bodies.
        finally:
            if self.client is client and mode == 'voice' and self.voice_conversation is not None:
                if success:
                    self.voice_idle_deadline = time.monotonic() + self.VOICE_HISTORY_IDLE_SECONDS
                else:
                    conversation = self._detach_voice_conversation()
                    with suppress(Exception):await conversation.end()
            if received and self.client is client:
                with suppress(OSError, DeviceError, asyncio.TimeoutError):
                    if not success and capabilities.get('capture_failure_detail') == 1 and detail in CAPTURE_FAILURE_DETAILS:
                        await client.capture_result(ticket, False, failure=detail)
                    elif not success and (failure=='no_speech' or (mode=='voice' and mode_valid)):
                        await client.capture_result(ticket,False,failure=failure)
                    else:await client.capture_result(ticket, success)

    async def refresh_read_request(self,client):
        request=await client.read_request_take()
        identity,ticket=request.get('identity'),request.get('ticket')
        if (type(identity) is not int or not 0<identity<=0xffffffff or
            type(ticket) is not int or not 0<ticket<=0xffffffff):return
        if self.screen_speech_task and not self.screen_speech_task.done():
            await client.read_request_finish(ticket,False);return
        self.screen_speech_task=asyncio.create_task(self._screen_speech(client,identity,ticket))

    async def _screen_speech(self,client,identity,ticket):
        try:
            result=await self.read_notification(identity,read_ticket=ticket)
            success=result.get('status')=='finished'
        except asyncio.CancelledError:
            raise
        except Exception:
            success=False
        try:
            async with self.lock:
                if self.client is client:await client.read_request_finish(ticket,success)
        except (OSError,DeviceError):
            pass # A disconnected board drops the volatile request.

    async def process_touch(self, event):
        if event["action"] == "open_session":
            accepted = False
            try:
                args = await asyncio.to_thread(self.notifications.cached_notification_launch_args, event["id"])
                if args:
                    accepted = await asyncio.to_thread(launch_notification, args)
            except (OSError, sqlite3.Error, subprocess.SubprocessError):
                pass  # Report failure to the board without dropping the connection.
            await self.client.finish_session_open(event, accepted=accepted)
            return
        self.notification_view.touch(event, channel=getattr(self.client, "channel", None))
        if event["action"] == "notice_open":
            item = self.notifications.latest_unseen()
            if item:
                try:
                    result = await self.client.show_text(notification_text(item), category=item["category"])
                except ValueError:
                    result = await self.client.show_text(notification_text(item, detail=False), category=item["category"])
                if result.get("status") == "shown":
                    try:
                        await self.client.show_notice_detail(item["id"])
                    except CommandRejected as error:
                        # The toast can expire between the two commands.
                        if error.code != "notice_not_visible":
                            raise
        elif event["action"] == "notice_seen" and "notification_id" in event:
            await asyncio.to_thread(self.notifications.mark_seen, event["notification_id"])
        if self.touches.full():
            self.touches.get_nowait()
            self.dropped_touches += 1
        touch = {"id": event["id"], "action": event["action"], "received_at": time.time()}
        for key in ("x", "y", "notification_id"):
            if key in event:
                touch[key] = event[key]
        self.touches.put_nowait(touch)

    async def render_metrics(self, reset):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("render_metrics") != 1:
                return {"status": "unsupported"}
            reply = await self.client.render_metrics(reset=reset)
        elapsed, frames = reply["elapsed_ms"], reply["frames"]
        result = {"status": "ok", "elapsed_ms": elapsed, "frames": frames,
                  "fps": round(frames * 1000 / elapsed, 2) if elapsed else None}
        if "displayed_frames" in reply:
            displayed = reply["displayed_frames"]
            result.update(displayed_frames=displayed,
                          displayed_fps=round(displayed * 1000 / elapsed, 2) if elapsed else None)
        for field in ("scene_redraws", "strips_written", "strips_skipped", "loop_stack_min_bytes",
                      "heap_free_bytes", "heap_largest_bytes", "background_cached"):
            if field in reply:
                result[field] = reply[field]
        for field in ("load_us", "background_us", "composite_us", "display_us",
                      "frame_us", "touch_poll_us", "touch_ack_ms", "spi_us", "displayed_frame_gap_ms",
                      "touch_dispatch_ms", "touch_role_feedback_ms"):
            if field not in reply:
                continue
            count, total, maximum = reply[field]
            result[field] = {"samples": count, "total": total, "max": maximum,
                             "mean": round(total / count, 2) if count else None}
        return result

    async def sync_clock(self, offset_minutes):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("clock_sync") != 1:
                return {"status": "unsupported"}
            reply = await self.client.sync_clock(offset_minutes)
            return {"status": "synced" if reply.get("synced") else "unknown"}

    async def clock_edit(self, mode, field, step):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("clock_edit") != 1:
                return {"status": "unsupported"}
            reply = await self.client.clock_edit(mode, field=field, step=step)
            fields = ("editing", "unix_seconds", "offset_minutes", "field", "draft_unix_seconds", "draft")
            return {"status": "ok", **{key: reply[key] for key in fields if key in reply}}

    async def set_view(self, page, make_default, clock_face):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("screen_views") != 1:
                return {"status": "unsupported"}
            await self.client.set_view(page, make_default=make_default, clock_face=clock_face)
            return {"status": "shown", "page": page}

    async def voice_names(self, *, user_name=None, device_name=None):
        from .voice_names import validate_voice_name
        async with self.lock:
            if self.client is None:return {'status':'not_connected','connection':self.snapshot}
            if self.client.channel.capabilities.get('voice_names')!=1:return {'status':'unsupported'}
            reply=await self.client.voice_names(user_name=user_name,device_name=device_name)
            try:
                user=validate_voice_name(reply.get('user_name'),empty_ok=True)
                device=validate_voice_name(reply.get('device_name'),empty_ok=False)
            except (ValueError,AttributeError):raise DeviceError('invalid_voice_names_reply') from None
            if ((user_name is not None and user!=user_name) or
                    (device_name is not None and device!=device_name)):
                raise DeviceError('voice_names_not_confirmed')
            return {'status':'ok','user_name':user,'device_name':device}

    async def voice_tool_settings(self, enabled=None):
        """Inspect or explicitly replace this pairing's voice tool permissions."""
        async with self.storage_lock:
            if enabled is not None:
                async with self.voice_permissions.lock():self.voice_permissions.update(enabled)
        client = self.client
        sync = None
        if enabled is not None:
            async with self.lock:
                client = self.client
                sync = 'pending' if client is None else await self.sync_onboard_voice_permissions(client)
        active, status = self.voice_permissions.inspect()
        capabilities = client.channel.capabilities if client is not None else None
        result = {'status': status, 'enabled': sorted(active), 'available': [
            {'name': name, 'description': description, 'requires_confirmation': voice_tool_requires_confirmation(name),
             'device_supported': None if capabilities is None else voice_tool_supported(name, capabilities)}
            for name, description in VOICE_TOOLS.items()
        ]}
        if sync is not None:
            result['onboard_sync'] = sync
        return result

    def _matches_voice_pairing(self, client, config):
        paired = client.config
        return (config.identity == paired.identity and config.key == paired.key and
                (config.device_id is None or config.device_id == client.device_id))

    async def sync_onboard_voice_permissions(self, client):
        if client.channel.capabilities.get('assistant_onboard') != 1:
            return None
        try:
            if (getattr(getattr(client, 'link', None), 'wireless', False) is not True or
                    not self._matches_voice_pairing(client, DeviceConfig.load(self.path))):
                return 'pending'
            enabled = frozenset(name for name in self.voice_permissions.enabled()
                                if name not in BOARD_READOUTS or client.channel.capabilities.get(name) == 1)
            sent = (client, client.channel.epoch, enabled)
            if self.voice_policy_sent != sent:
                self.voice_policy_sent = None
                await client.send_voice_permissions(enabled)
                self.voice_policy_sent = sent
        except LINK_ERRORS:
            return 'pending'
        return 'applied'

    async def agent_config(self, patch):
        async with self.lock:
            client = self.client
            if client is None:
                return {'status': 'not_connected'}
            if client.channel.capabilities.get('assistant_onboard') != 1:
                return {'status': 'unsupported'}
            if patch is None:
                try:
                    return {'status': 'ok', **await client.agent_config_status()}
                except LINK_ERRORS:
                    return {'status': 'unavailable'}
            if getattr(getattr(client, 'link', None), 'wireless', False) is not True:
                return {'status': 'requires_paired_wifi'}
            try:
                config = DeviceConfig.load(self.path)
                if not self._matches_voice_pairing(client, config):
                    return {'status': 'pairing_changed'}
            except (OSError, ValueError):
                return {'status': 'pairing_changed'}
            try:
                tx = await client.apply_agent_config(patch)
            except AgentConfigUnconfirmed as error:
                return await self._resolve_agent_commit(client, config, error.tx)
            except CommandRejected:
                return {'status': 'rejected'}
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'not_applied'}
            return {'status': 'applied', 'tx': tx}

    async def _resolve_agent_commit(self, client, config, tx):
        try:
            async with asyncio.timeout(STATUS_WIFI_RECOVERY_TIMEOUT):
                if DeviceConfig.load(self.path) != config:
                    return {'status': 'unknown', 'tx': tx}
                if client.channel.online:
                    status = await client.agent_config_status()
                else:
                    await self.drop()
                    if DeviceConfig.load(self.path) != config:
                        return {'status': 'unknown', 'tx': tx}
                    async with self.client_factory(config) as replacement:
                        if not self._matches_voice_pairing(replacement, config):
                            return {'status': 'unknown', 'tx': tx}
                        status = await replacement.agent_config_status()
                if DeviceConfig.load(self.path) == config and status['last_applied_tx'] == tx:
                    return {'status': 'applied', 'tx': tx}
        except LINK_ERRORS:
            pass
        return {'status': 'unknown', 'tx': tx}

    async def voice_battery_status(self):
        async with self.lock:
            client = self.client
            if client is None or client is not self.voice_active_client:
                return {'status': 'not_connected'}
            if client.channel.capabilities.get('battery_status') != 1:
                return {'status': 'unsupported'}
            try:
                reply = await client.status()
            except LINK_ERRORS:
                return {'status': 'unavailable'}
            return {'status': 'ok', 'battery': battery_view(reply.get('battery'))}

    async def voice_board_readout(self, name):
        async with self.lock:
            client = self.client
            if client is None or client is not self.voice_active_client:
                return {'status': 'not_connected'}
            if client.channel.capabilities.get(name) != 1:
                return {'status': 'unsupported'}
            try:
                return await client.read_board_status(name)
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unavailable'}

    async def voice_device_settings(self, *, volume, brightness):
        async with self.lock:
            client, ticket = self.client, self.voice_active_ticket
            if client is None or client is not self.voice_active_client or ticket is None:
                return {'status': 'not_connected'}
            if client.channel.capabilities.get('device_settings') != 1:
                return {'status': 'unsupported'}
            changing = volume is not None or brightness is not None
            try:
                result = await client.voice_device_settings(volume=volume, brightness=brightness, capture_ticket=ticket)
            except CommandRejected as error:
                if changing and error.code in ('settings_partial', 'settings_timeout'):
                    return {'status': 'unknown', 'reason': error.code}
                return {'status': 'rejected'}
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unknown' if changing else 'unavailable'}
            return {'status': 'ok', **result}

    async def voice_restart(self):
        async with self.lock:
            client, ticket = self.client, self.voice_active_ticket
            if client is None or client is not self.voice_active_client or ticket is None:
                return {'status': 'not_connected'}
            if client.channel.capabilities.get('system_restart') != 1:
                return {'status': 'unsupported'}
            fields = {'capture_ticket': ticket} if client.channel.capabilities.get('assistant_onboard') == 1 else {}
            try:
                return await client.restart(confirm=True, **fields)
            except CommandRejected:
                return {'status': 'rejected'}
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unknown'}

    async def _voice_media_tool(self, capability, changing, invoke):
        async with self.lock:
            client, ticket = self.client, self.voice_active_ticket
            if client is None or client is not self.voice_active_client or ticket is None:
                return {'status': 'not_connected'}
            if not voice_tool_supported(capability, client.channel.capabilities):
                return {'status': 'unsupported'}
            try:
                result = await invoke(client, ticket)
            except CommandRejected as error:
                if changing and error.code in ('media_timeout', 'media_partial', 'out_of_memory'):
                    return {'status': 'unknown'}
                return {'status': 'rejected', 'reason': error.code}
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unknown' if changing else 'unavailable'}
            return {'status': 'ok', **result}

    async def voice_music_control(self, action, path):
        return await self._voice_media_tool('music_control', action != 'status',
            lambda client, ticket: client.music_control(action, path=path, capture_ticket=ticket))

    async def voice_photo_slideshow(self, mode, interval_seconds):
        return await self._voice_media_tool('photo_slideshow', True,
            lambda client, ticket: client.photo_album(mode, interval_seconds=interval_seconds, capture_ticket=ticket))

    async def voice_role_action(self, action, duration_ms):
        """Show the role and play only while this exact voice request is active."""
        ticket=self.voice_active_ticket
        expected=self.voice_active_client
        if ticket is None:return {'status':'busy'}
        async with self.lock:
            client=self.client
            if client is None or client is not expected:return {'status':'not_connected'}
            if client.channel.capabilities.get('capture_voice_role')!=1 or client.channel.capabilities.get('role_actions')!=1:return {'status':'unsupported'}
            try:
                actions = await client.list_actions()
                if action not in actions:
                    return {'status': 'unsupported', 'action': action}
                await client.capture_role_view(ticket)
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unavailable', 'action': action}
            try:
                result = await client.capture_role_play(ticket, actions.index(action), duration_ms or 0)
            except CommandRejected as error:
                status = 'unknown' if error.code in ('media_timeout', 'out_of_memory') else 'unavailable'
                return {'status': status, 'action': action}
            except (OSError, DeviceError, TimeoutError):
                return {'status': 'unknown', 'action': action}
            reply={'status':result['action_status'],'action':action}
            if isinstance(result.get('reason'),str) and result['reason']:reply['reason']=result['reason']
            return reply

    async def dashboard_config(self,metric,source,rotation=None,interval=None):
        if sum(value is not None for value in (metric,source,rotation,interval))>1:
            raise ValueError('请分别设置数据项目、额度来源、轮播项目和间隔。')
        async with self.lock:
            if self.client is None:return {'status':'not_connected','connection':self.snapshot}
            if self.client.channel.capabilities.get('dashboard_config')!=1:return {'status':'unsupported'}
            if rotation is not None and self.client.channel.capabilities.get('dashboard_rotation')!=1:return {'status':'unsupported'}
            if interval is not None and self.client.channel.capabilities.get('dashboard_interval')!=1:return {'status':'unsupported'}
            if source is not None:await self.client.select_quota_source(source)
            reply=await self.client.dashboard_config(metric,rotation=rotation,interval=interval)
            current,quota=reply.get('metric'),reply.get('quota_source')
            if (type(current) is not int or not 0<=current<len(DASHBOARD_METRICS) or
                type(quota) is not int or not 0<=quota<len(SOURCES)):
                raise DeviceError('invalid_dashboard_config_reply')
            result={'status':'ok','metric':DASHBOARD_METRICS[current],'quota_source':SOURCES[quota]}
            if self.client.channel.capabilities.get('dashboard_rotation')==1:
                mask=reply.get('rotation')
                if type(mask) is not int or not 1<=mask<=15:raise DeviceError('invalid_dashboard_rotation_reply')
                result['rotation']=[name for index,name in enumerate(DASHBOARD_ROTATION) if mask & (1<<index)]
            if self.client.channel.capabilities.get('dashboard_interval')==1:
                seconds=reply.get('interval')
                if type(seconds) is not int or seconds not in DASHBOARD_INTERVALS:raise DeviceError('invalid_dashboard_interval_reply')
                result['interval_seconds']=seconds
            return result

    async def appearance_config(self,theme,wallpaper):
        async with self.lock:
            if self.client is None:return {'status':'not_connected','connection':self.snapshot}
            if self.client.channel.capabilities.get('appearance_config')!=1:return {'status':'unsupported'}
            reply=await self.client.appearance_config(theme=theme,wallpaper=wallpaper)
            current,path=reply.get('theme'),reply.get('wallpaper')
            try:
                _appearance_theme(current)
                if path=='':relative=''
                elif isinstance(path,str) and path.startswith('/sdcard/Photos/'):relative='photos/'+path[15:]
                elif isinstance(path,str) and path.startswith('/sdcard/Media/'):relative='media/'+path[14:]
                elif isinstance(path,str) and path.startswith('/sdcard/Wallpapers/'):relative='wallpapers/'+path[19:]
                else:raise ValueError('invalid path')
                if _appearance_wallpaper(relative)!=path:raise ValueError('invalid path')
            except (TypeError,ValueError,UnicodeError):raise DeviceError('invalid_appearance_config_reply') from None
            return {'status':'ok','theme':current,'wallpaper':relative}

    async def publish_weather(self, snapshot, location, *, background_client=None):
        async with self.lock:
            client = self.client
            if client is None or (background_client is not None and client is not background_client):
                return {"status": "not_connected", "connection": self.snapshot}
            if client.channel.capabilities.get("weather") != 1:
                return {"status": "unsupported"}
            if background_client is not None and client.channel.capabilities.get("weather_device") == 1:
                if (await client.status()).get("weather_device_refresh") is True:
                    return {"status": "device_managed"}
            reply = await client.set_weather(snapshot, location=location)
        self.weather_due = time.monotonic() + 1800
        return {"status": "updated", "updated": reply["updated"]}

    async def set_weather_location(self, latitude, longitude, label):
        location = dict(latitude=latitude, longitude=longitude, label=label)
        async with self.weather_lock:
            client = self.client
            relay = client is not None and client.channel.capabilities.get("weather_relay") == 1
            snapshot = await asyncio.to_thread(fetch_relay_weather, location) if relay else await asyncio.to_thread(fetch_weather, **location)
            self.weather_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.weather_path.parent, delete=False) as stream:
                pending = Path(stream.name)
                try:
                    json.dump(location, stream)
                    stream.flush()
                    os.replace(pending, self.weather_path)
                finally:
                    pending.unlink(missing_ok=True)
            if relay:
                return await self.publish_relay_weather(snapshot, background_client=client)
            return await self.publish_weather(snapshot, location)

    async def publish_relay_weather(self, snapshot, *, background_client):
        async with self.lock:
            if self.client is not background_client:
                return {"status": "not_connected", "connection": self.snapshot}
            await background_client.relay_weather(snapshot)
        self.weather_due = time.monotonic() + 1800
        return {"status": "updated"}

    async def refresh_weather(self):
        async with self.weather_lock:
            client = self.client
            relay = client is not None and client.channel.capabilities.get("weather_relay") == 1
            if (client is None or (not relay and client.channel.capabilities.get("weather") != 1) or
                    self.snapshot.get("connection") != "connected" or
                    self.snapshot.get("weather", {}).get("device_refresh") is True or
                    time.monotonic() < self.weather_due):
                return
            self.weather_due = time.monotonic() + 300
            try:
                try:
                    location = json.loads(self.weather_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return
                if relay:
                    snapshot = await asyncio.to_thread(fetch_relay_weather, location)
                    await self.publish_relay_weather(snapshot, background_client=client)
                else:
                    snapshot = await asyncio.to_thread(fetch_weather, **location)
                    await self.publish_weather(snapshot, location, background_client=client)
            except (TypeError, *LINK_ERRORS):
                pass

    async def poll_weather(self):
        while True:
            await self.refresh_weather()
            await asyncio.sleep(30)

    async def poll_tokens(self):
        runtime = None
        pending = None
        snapshot_client = None
        snapshot_epoch = None
        verified_epoch = None
        legacy_quota_due = 0.0
        last_connected_at = None
        last_windows = None
        last_frame = None
        burst = SpeedBurst()
        next_publish = 0.0

        async def publish(client, windows, snapshot, adaptive, sampled_at, captured_at, state, style, rates):
            nonlocal snapshot_client, snapshot_epoch, verified_epoch, legacy_quota_due
            async with self.lock:
                if (client is not self.client or windows != self.snapshot.get("speed_windows")
                        or time.monotonic() - sampled_at >= 3):
                    return
                if client.channel.capabilities.get("speed_snapshot") == 1 and snapshot["sessions"] is not None:
                    if (snapshot_client is not client or snapshot_epoch != client.channel.epoch or
                            (client.channel.capabilities.get("quota_sources") != 1 and
                             time.monotonic() >= legacy_quota_due)):
                        await client.update_tokens(snapshot, windows=windows)
                        snapshot_client = client
                        snapshot_epoch = client.channel.epoch
                        legacy_quota_due = time.monotonic() + 10
                    if not await client.update_speed_history(captured_at=captured_at, snapshot=snapshot,
                                                             state=state, style=style, rates=rates,
                                                             verify=verified_epoch != (client, client.channel.epoch)):
                        return
                    verified_epoch = (client, client.channel.epoch)
                else:
                    await client.update_tokens(snapshot, windows=windows)
                    snapshot_client = None
                    snapshot_epoch = None
                self.token_snapshot = {**snapshot, "adaptive": adaptive}
                self.token_published_at = time.monotonic()
                loss = self.last_link_loss
                if (loss is not None and loss["speed_was_active"]
                        and "connection_recovery_ms" in loss and "speed_recovery_ms" not in loss):
                    loss["speed_recovery_ms"] = self._elapsed_since_link_loss()
                    self.snapshot["last_link_loss"] = loss.copy()

        try:
            while True:
                cycle_started = time.monotonic()
                client = self.client
                windows = self.snapshot.get("speed_windows")
                connected = (client is not None and client.channel.capabilities.get("token_stats") == 1 and
                             (windows is not None or client.channel.capabilities.get("speed_windows") != 1))
                if connected:
                    last_connected_at = cycle_started
                    last_windows = windows
                offline_grace = (client is None and last_connected_at is not None and
                                 cycle_started - last_connected_at < 30)
                if offline_grace:
                    windows = last_windows
                if connected or offline_grace:
                    try:
                        if self.probe_collector is not None:
                            snapshot = await self.probe_collector.sample()
                            metric_version = self.probe_collector.metric_version
                            speed_state = self.probe_collector.speed_state
                        elif self.reference_collector is not None:
                            snapshot = await self.reference_collector.sample()
                            metric_version = "reference-output-v1"
                            source = (self.reference_collector.latest or {}).get("total", {})
                            speed_state = "E" if source.get("speed_state") in ("estimated", "partial") else None
                        else:
                            # A session log does not contain an equivalent live rate.
                            # Leave it unavailable rather than silently changing the metric.
                            self.token_snapshot = None
                            await asyncio.sleep(1)
                            continue
                        if snapshot is None:
                            self.token_snapshot = None
                            await asyncio.sleep(1)
                            continue
                        now = time.time()
                        if runtime is None:
                            runtime = AdaptiveRuntime(History(self.token_history_path))
                        runtime.configure(windows)
                        speed_state = speed_state if speed_state in ("L", "E", "I") else (
                            "L" if snapshot["rate10"] else "I")
                        collector = self.probe_collector or self.reference_collector
                        latest = getattr(collector, "latest", None)
                        stamp = (latest.get("t") if self.probe_collector else latest.get("captured_at_ms")) if isinstance(latest, dict) else None
                        captured_at = stamp / 1000 if type(stamp) in (int, float) else now
                        frame = Frame(str(int(captured_at * 1000)), captured_at, snapshot["rate10"], speed_state, metric_version)
                        frame_key = (metric_version, frame.sample_id)
                        if frame_key != last_frame:
                            runtime.history.add(frame, now)
                            last_frame = frame_key
                        runtime.observe(frame, now)
                        await runtime.tick()
                        style = runtime.wire()
                        snapshot["gear"] = (style or {}).get("current", 0)
                        burst_now = int(time.monotonic() * 1000)
                        burst.update(snapshot["gear"], burst_now)
                        burst.report(frame.sample_id, snapshot["rate10"], speed_state, snapshot["gear"], burst_now)
                        if client is not None and client.channel.capabilities.get("speed_burst") == 1:
                            level = style["overall"] if style and style.get("calibrated") else 0
                            snapshot = {**snapshot, **burst.wire(burst_now), "speed_level": level}
                        rates = runtime.history.recent_rates(metric_version, int(now))
                        allowance = self.codex_allowance
                        current = [row for row in allowance.get("quotas", []) if row[2] > now]
                        if current:
                            snapshot["quotas"] = current
                        for key in ("credits", "reset_cards"):
                            if key in allowance:
                                snapshot[key] = allowance[key]
                        if client is not None and client.channel.capabilities.get("token_quality") == 1:
                            snapshot["speed_state"] = speed_state
                        if connected and cycle_started >= next_publish and (pending is None or pending.done()):
                            if pending is not None:
                                try:
                                    pending.result()
                                except LINK_ERRORS:
                                    pass
                            pending = asyncio.create_task(publish(client, windows, snapshot, runtime.status(),
                                                                  time.monotonic(), captured_at, speed_state,
                                                                  style, rates))
                            next_publish = cycle_started + 1
                    except (sqlite3.Error, *LINK_ERRORS):
                        pass
                await asyncio.sleep(max(0, .5 - (time.monotonic() - cycle_started)))
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if self.reference_collector is not None:
                await self.reference_collector.close()
            if self.owns_probe_collector:
                await self.probe_collector.close()
            if runtime is not None:
                await runtime.close()
                runtime.history.close()

    async def refresh_quota(self):
        try:
            allowance = await fetch_quota()
        except (AttributeError, TypeError, *LINK_ERRORS):
            allowance = None
        updated = isinstance(allowance, dict)
        now = time.time()
        if updated:
            self.codex_live_allowance = allowance.copy()
            self.codex_live_observed_at = now
        if not updated:
            allowance = self.codex_allowance
        if (self.reference_collector is not None or self.probe_collector is not None) and not any(
                row[2] > now for row in allowance.get("quotas", [])):
            try:
                local = await asyncio.to_thread(self.token_metrics.sample)
                allowance = {**allowance, "quotas": local["quotas"]}
            except LINK_ERRORS:
                pass
        self.codex_allowance = allowance
        return updated

    async def poll_quota(self):
        while True:
            if self.client is None or self.client.channel.capabilities.get("token_stats") != 1:
                await asyncio.sleep(10)
                continue
            updated = await self.refresh_quota()
            await asyncio.sleep(300 if updated else 10)

    def quota_display_sources(self):
        client = self.client
        if client is None or client.channel.capabilities.get("quota_sources") != 1:
            return ()
        if client.channel.capabilities.get("quota_multi") == 1:
            return SOURCES
        selected = self.snapshot.get("quota_source")
        return (selected,) if selected in SOURCES else ()

    async def poll_analytics(self):
        """Collect and sync history separately from reminder and speed polling."""
        while True:
            client = self.client
            if client is not None and client.channel.capabilities.get("agent_analytics") == 1:
                try:
                    await self.analytics.step(client, quota_store=self.quota_store,
                                              codex=(self.codex_live_observed_at, self.codex_live_allowance))
                except (OSError, sqlite3.Error, ValueError, KeyError, *LINK_ERRORS) as error:
                    self.snapshot["analytics_error"] = type(error).__name__
                else:
                    self.snapshot.pop("analytics_error", None)
            await asyncio.sleep(4)

    async def poll_antigravity_quota(self):
        """Refresh the shared quota cache without delaying writes to the board."""
        while True:
            eligible = any(source in ANTIGRAVITY_WINDOWS for source in self.quota_display_sources())
            if eligible:
                with suppress(OSError, sqlite3.Error, ValueError):
                    await asyncio.to_thread(self.quota_store.refresh_antigravity)
            await asyncio.sleep(60 if eligible else 5)

    async def poll_quota_display(self):
        """Keep each display feed fresh independently of the live speed probe."""
        last_client, last_sources = None, ()
        sent = {}
        while True:
            client, sources = self.client, self.quota_display_sources()
            if client is not last_client or sources != last_sources:
                sent.clear()
                last_client, last_sources = client, sources
            if sources:
                now = time.time()
                claude = (await asyncio.to_thread(self.quota_store.read, "claude_code", now)
                          if "claude_code" in sources else None)
                antigravity = (await asyncio.to_thread(self.quota_store.read, "antigravity", now)
                               if any(source in ANTIGRAVITY_WINDOWS for source in sources) else None)
                codex_age = now - self.codex_live_observed_at
                codex = self.codex_live_allowance if 0 <= codex_age <= MAX_CODEX_ALLOWANCE_AGE else None
                pending = []
                for source in sources:
                    reading = project_quota(source, codex=codex, claude=claude, antigravity=antigravity, now=now)
                    previous, due = sent.get(source, (None, 0.0))
                    if reading != previous or time.monotonic() >= due:
                        pending.append((source, reading))
                if pending:
                    async with self.lock:
                        for source, reading in pending:
                            if client is not self.client or source not in self.quota_display_sources():
                                break
                            try:
                                await client.update_quota(reading)
                            except CommandRejected:
                                sent.pop(source, None)
                            except LINK_ERRORS:
                                sent.pop(source, None)
                                break
                            else:
                                sent[source] = reading, time.monotonic() + 5
            await asyncio.sleep(1)

    async def configure_computer_metrics(self, *, cpu=None, memory=None):
        async with self.lock:
            client = self.client
            if cpu is not None or memory is not None:
                async with self.computer_metrics_lock:
                    await asyncio.to_thread(self.computer_display.update, cpu=cpu, memory=memory, queue=True)
                if client is not None and client.channel.capabilities.get("computer_preferences") == 1:
                    await self.sync_computer_preferences(client, await client.status(compact=True))
                self.computer_metrics_changed.set()
            enabled = self.computer_display.get()
        supported = client.channel.capabilities.get("computer_metrics") == 1 if client is not None else None
        return {"enabled": enabled, "device_supported": supported}

    async def sync_computer_preferences(self, client, heartbeat):
        if client.channel.capabilities.get("computer_preferences") != 1:
            return
        state = heartbeat.get("computer_prefs")
        if state is not None and (type(state) is not int or not 0 <= state <= 0xffffffff):
            raise DeviceError("invalid_computer_preferences")
        async with self.computer_metrics_lock:
            previous = self.computer_display.get()
            previous_client = self.computer_preference_client
            pending = self.computer_display.pending_fields
            if state is not None:
                # Preserve the computer's explicitly queued choices and adopt the
                # board's latest values for the other switches.
                device = {"cpu": bool(state & 1), "memory": bool(state & 2)}
                merged = {key: previous[key] if key in pending else value for key, value in device.items()}
                await asyncio.to_thread(self.computer_display.update, **merged)
            if state is not None and state & 4:
                await client.acknowledge_computer_preferences(state)
            if pending:
                enabled = self.computer_display.get()
                await client.set_computer_preferences(cpu_enabled=enabled["cpu"], memory_enabled=enabled["memory"])
                await asyncio.to_thread(self.computer_display.update, synchronized=True)
            self.computer_preference_client = client
            if previous != self.computer_display.get() or previous_client is not client:
                self.computer_metrics_changed.set()

    async def poll_computer_metrics(self):
        sent_client = None
        sent_enabled = None
        while True:
            self.computer_metrics_changed.clear()
            client = self.client
            enabled = self.computer_display.get()
            try:
                syncs_choices = client is not None and client.channel.capabilities.get("computer_preferences") == 1
                if (client is not None and client.channel.capabilities.get("computer_metrics") == 1 and
                    (not syncs_choices or self.computer_preference_client is client and not self.computer_display.pending_fields)):
                    if any(enabled.values()) or client is not sent_client or enabled != sent_enabled:
                        sample = await asyncio.to_thread(self.computer_metrics.sample, **enabled)
                        async with self.lock:
                            if (client is self.client and enabled == self.computer_display.get() and
                                    (not syncs_choices or not self.computer_display.pending_fields)):
                                await client.update_computer_metrics(
                                    cpu_enabled=enabled["cpu"], memory_enabled=enabled["memory"],
                                    cpu=sample.get("cpu"), memory=sample.get("memory"))
                                sent_client, sent_enabled = client, enabled
                else:
                    sent_client = sent_enabled = None
            except LINK_ERRORS:
                pass
            try:
                await asyncio.wait_for(self.computer_metrics_changed.wait(), 2)
            except asyncio.TimeoutError:
                pass

    def token_status(self):
        if self.no_token_probe:
            return {"status": "unavailable", "reason": "token_collection_disabled"}
        if self.token_snapshot is None or time.monotonic() - self.token_published_at >= 15:
            if self.probe_collector is None and self.reference_collector is None:
                return {"status": "unavailable", "reason": "agent_speed_probe_missing",
                        "next_step": "安装 AgentSpeedProbe，或在启动时指定 --token-probe。"}
            return {"status": "waiting"}
        return {"status": "ok", **self.token_snapshot}

    async def speed_windows(self, recent_seconds, long_days):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("speed_windows") != 1:
                return {"status": "unsupported"}
            reply = await self.client.speed_windows(recent_seconds, long_days)
            windows = speed_windows_view(reply)
            if windows is None:
                raise DeviceError("invalid_speed_windows_response")
            if self.snapshot.get("speed_windows") != windows:
                self.token_snapshot = None
                self.snapshot["speed_windows"] = windows
            return {"status": "ok", **windows}

    async def set_role_auto(self, enabled):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("token_stats") != 1:
                return {"status": "unsupported"}
            reply = await self.client.set_role_auto(enabled)
            return {"status": "saved", "enabled": reply.get("automatic_role")}

    async def diagnostics(self, include_card):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            capabilities = self.client.channel.capabilities
            result = {}
            legacy = capabilities.get("device_diagnostics") == 1
            if capabilities.get("device_info") == 1:
                try:
                    info = await self.client.device_info()
                except LINK_ERRORS:
                    if not legacy:
                        raise
                else:
                    self.device_info = (self.client, info)
                    if info and "diagnostics" in info:
                        self.snapshot["device_info"] = info
                        result["diagnostics"] = info["diagnostics"]
            if legacy:
                result.update(await self.client.diagnostics(include_card=include_card))
                return {"status": "ok", **result}
            return {"status": "ok", **result} if result else {"status": "unsupported"}

    async def restart(self, *, confirm=False):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("system_restart") != 1:
                return {"status": "unsupported"}
            try:
                return await self.client.restart(confirm=confirm)
            except (AckTimeout, OSError):
                return {"status": "unknown", "reason": "restart_ack_lost"}
            except CommandRejected as error:
                return {"status": "rejected", "reason": error.code}

    async def recover_subsystem(self, target):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("subsystem_recover") != 1:
                return {"status": "unsupported"}
            try:
                return await self.client.recover_subsystem(target)
            except (AckTimeout, OSError):
                return {"status": "unknown", "reason": "recovery_ack_lost"}
            except CommandRejected as error:
                return {"status": "rejected", "reason": error.code}

    async def reset_usb(self, port, device_id):
        async with self.storage_lock, self.lock:
            config = DeviceConfig.load(self.path)
            if config.device_id != device_id:
                raise DeviceError("device_identity_mismatch")
            await self.drop()
            reset = asyncio.create_task(asyncio.to_thread(reset_native_usb, port, config.identity))
            try:
                return await asyncio.shield(reset)
            except asyncio.CancelledError:
                with suppress(OSError, RuntimeError, ValueError):
                    await reset
                raise

    async def select_network(self, ssid):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("device_diagnostics") != 1:
                return {"status": "unsupported"}
            reply = await self.client.select_network(ssid)
            result = {"status": reply["network_status"]}
            if "operation" in reply:
                result.update(operation=reply["operation"], boot=reply["boot"])
            return result

    async def show_text(self, text):
        if self.client is None:
            return self.snapshot
        async with self.lock:
            if self.client is None:
                return self.snapshot
            try:
                return await self.client.show_text(text)
            finally:
                if not self.client.channel.online:
                    await self.drop()
                    self.snapshot = {"connection": "offline"}

    def _existing_pairing(self, identity):
        try:
            existing = DeviceConfig.load(self.path)
        except FileNotFoundError:
            return None
        if existing.identity != identity:
            raise ValueError("这台电脑已配对另一台设备，请选择添加设备。")
        return existing

    @staticmethod
    def _verify_existing_uuid(existing, observed):
        if existing is not None and existing.device_id is not None and existing.device_id != observed:
            raise ValueError("设备标识与已保存的配对不符。")

    async def pair(self, code, address):
        config = DeviceConfig.from_code(code, address)
        async with self.storage_lock, self.lock:
            existing = self._existing_pairing(config.identity)
            device_id = await self._verify_pairing(config)
            self._verify_existing_uuid(existing, device_id)
            if device_id is not None:
                config = replace(config, device_id=device_id)
            if self.identity_registry is not None and Path(self.path) == self.identity_registry.default_path:
                self.identity_registry.save_default(config, existing)
            else:
                config.save(self.path)
        return {"device": config.identity, **({"device_id": config.device_id} if config.device_id else {})}

    async def _verify_pairing(self, config, usb_port=None):
        await self.drop()
        self.snapshot = {"connection": "connecting"}
        if usb_port is None or config.address is not None:
            try:
                async with asyncio.timeout(WIFI_FALLBACK_TIMEOUT if usb_port is not None else None):
                    async with self.client_factory(config) as board:
                        await board.status()
                        device_id = board.device_id
            except (OSError, AckTimeout):
                if usb_port is None:
                    raise
            else:
                return device_id
        async with self.usb_client(config, usb_port) as board:
            await board.status()
            return board.device_id

    async def pair_usb(self, port, expected_identity, credentials_file):
        registration = UsbPort.register(port)
        async with self.storage_lock, self.lock:
            existing = self._existing_pairing(expected_identity)
            registration.verify()
            # A live controller can keep USB setup busy even when this same
            # MCP instance owns the connection. Release it before opening USB.
            await self.drop()
            config = await asyncio.to_thread(probe_usb, port, expected_identity, credentials_file)
            registration.verify()
            device_id = await self._verify_pairing(config, usb_port=registration)
            self._verify_existing_uuid(existing, device_id)
            if device_id is not None:
                config = replace(config, device_id=device_id)
            if self.identity_registry is not None and Path(self.path) == self.identity_registry.default_path:
                self.identity_registry.save_default(config, existing)
            else:
                config.save(self.path)
            self.usb_port = registration
        return {"device": config.identity, **({"device_id": config.device_id} if config.device_id else {})}

    def pair_from_portal(self, code, address):
        future = asyncio.run_coroutine_threadsafe(self.pair(code, address), self.loop)
        try:
            return future.result(timeout=30)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("pairing_timeout") from None

    async def upload_files(self, items, *, expected_client=None):
        if not 1 <= len(items) <= 100:
            raise ValueError("请选择 1 至 100 个文件。")
        checked, destinations = [], set()
        for item in items:
            source = Path(item.source).expanduser()
            validate_path(item.path)
            if not source.is_absolute() or not source.is_file():
                raise ValueError("每个源文件必须是本机存在的绝对路径。")
            if not 0 < source.stat().st_size <= MAX_FILE_BYTES:
                raise ValueError("文件为空或超过 128 MiB。")
            if item.path.casefold() in destinations:
                raise ValueError("多个文件不能使用相同的储存卡路径。")
            destinations.add(item.path.casefold())
            checked.append((source, item.path))
        async with self.storage_lock:
            client = self.client
            if client is None:
                return {"status": "not_connected", "completed": [], "connection": self.snapshot}
            if expected_client is not None and client is not expected_client:
                return {"status": "connection_changed", "completed": [], "connection": self.snapshot}
            if client.channel.capabilities.get('card_restricted_paths')==1:
                def supported(path):
                    return (client.channel.capabilities.get('music_library') == 1 and path.startswith(MUSIC_DIRECTORIES) or
                            client.channel.capabilities.get('photo_jpeg_upload')==1 and re.fullmatch(r'photos/p-[0-9a-f]{16}\.jpg',path) or
                            client.channel.capabilities.get('wallpaper_jpeg_upload')==1 and re.fullmatch(r'wallpapers/[A-Za-z0-9][A-Za-z0-9_-]{0,23}-[0-9a-f]{16}\.jpg',path) or
                            client.channel.capabilities.get('theme_json_upload')==1 and re.fullmatch(r'themes/[A-Za-z0-9][A-Za-z0-9_-]{0,23}-[0-9a-f]{16}\.json',path))
                if any(not supported(path) for _,path in checked):
                    raise ValueError('此设备请用 desk_add_photo 导入图片、desk_add_wallpaper 导入壁纸、desk_add_theme 导入主题，或用 desk_install_role 安装角色。')
            async with self._card_display_batch(client, enabled=len(checked) > 1):
                completed = []
                for source, path in checked:
                    try:
                        client, transfer = await self._upload_card_file(client, path, source)
                        completed.append(transfer)
                    except LINK_ERRORS as error:
                        await self._drop_failed_upload(client)
                        return {"status": "failed", "completed": completed,
                                "failed": {"path": path, "reason": str(error)}}
                return {"status": "completed", "completed": completed}

    async def delete_resource(self, path):
        async with self.storage_lock, self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("card_delete") != 1:
                return {"status": "unsupported"}
            reply = await self.client.delete_resource(path)
            return {"path": path, "deleted": reply["deleted"]}

    async def _reconnect_card_wifi(self, previous, deadline):
        # Call with self.lock held. The background poll may have already
        # replaced the failed connection while the file transfer was running.
        current = self.client
        if current is not None and current is not previous:
            if (current.config.identity != previous.config.identity or
                    current.config.key != previous.config.key or current.device_id != previous.device_id or
                    not current.link.wireless):
                raise DeviceError("connection_changed")
            if current.channel.online:
                return current

        await self.drop()
        self.snapshot = {"connection": "connecting"}
        try:
            async with asyncio.timeout_at(deadline):
                while self.client is None:
                    try:
                        self.client = await self.open_wifi(previous.config)
                    except DeviceBusyError:
                        if not request_worker_yield(self.path):
                            raise
                        await asyncio.sleep(.25)
        except LINK_ERRORS:
            self.snapshot = {"connection": "offline"}
            raise
        if self.client.device_id != previous.device_id:
            await self.drop()
            self.snapshot = {"connection": "offline"}
            raise DeviceError("device_identity_mismatch")
        self.last_notice_count = self.last_message_cache = None
        self.record_status(self.client.channel.capabilities)
        return self.client

    async def wait_role(self, identity):
        deadline = asyncio.get_running_loop().time() + 120
        client = self.client
        reconnected = False
        while asyncio.get_running_loop().time() < deadline:
            wireless = getattr(getattr(client, "link", None), "wireless", False) is True
            compact = wireless and client.channel.capabilities.get("compact_status") == 1
            try:
                status = await client.status(compact=True) if compact else await client.status()
            except (AckTimeout, OSError):
                if reconnected or not wireless:
                    raise
                # The activation was acknowledged. Reconnect to observe its
                # result without sending another command to change the role.
                boot = client.channel.boot
                client = await self._reconnect_card_wifi(client, deadline)
                if client.channel.boot != boot:
                    return {"status": "activation_unconfirmed", "role": identity, "reason": "device_restarted"}
                reconnected = True
                continue
            if status.get("role_state") == "ready" and status.get("role_id") == identity:
                return {"status": "ready", "role": identity,
                        "remembered_after_restart": status.get("role_persisted")}
            if status.get("role_state") == "failed":
                result = {"status": "activation_failed", "role": identity}
                if isinstance(stage := status.get("role_failure_stage"), str):
                    result["stage"] = stage
                if status.get("role_rollback_ok") is False:
                    result["role_rollback_ok"] = False
                return result
            await asyncio.sleep(1)
        return {"status": "activation_unconfirmed", "role": identity}

    async def install_role(self, source):
        with tempfile.TemporaryDirectory(prefix="deskpet-role-") as directory:
            bundle = await asyncio.to_thread(prepare_role_source, source, directory)
            return await self._install_role_bundle(bundle)

    async def _upload_card_file(self, client, path, source, *, expected_sha256=None):
        options = {} if expected_sha256 is None else {"expected_sha256": expected_sha256}
        if (getattr(getattr(client, "link", None), "wireless", False) is True and
                client.channel.capabilities.get("card_fetch_receipt") == 1):
            original_boot = client.channel.boot

            async def resume(_channel):
                nonlocal client
                async with self.lock:
                    deadline = asyncio.get_running_loop().time() + 30
                    recovered = await self._reconnect_card_wifi(client, deadline)
                    if recovered.channel.boot != original_boot:
                        await self.drop()
                        self.snapshot = {"connection": "offline"}
                        raise DeviceError("device_restarted")
                    if recovered.channel.capabilities.get("card_fetch_receipt") != 1:
                        raise DeviceError("card_fetch_receipt_not_supported")
                    client = recovered
                return client.channel

            # Receipt firmware resumes inside the original relay's deadline.
            # Do not fall through to the legacy path that starts a new worker.
            result = await client.upload_file(path, source, resume=resume, **options)
            return client, result
        try:
            result = await client.upload_file(path, source, **options)
            return client, result
        except (AckTimeout, OSError):
            wireless = getattr(getattr(client, "link", None), "wireless", False) is True
            if client.channel.online or not wireless or client.channel.capabilities.get("card_fetch_async") != 1:
                raise
            async with self.lock:
                deadline = asyncio.get_running_loop().time() + 30
                client = await self._reconnect_card_wifi(client, deadline)
        # The previous worker can still be writing after the control link drops.
        for delay in (0, 1, 2, 4, 8, 15):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await client.upload_file(path, source, **options)
                return client, result
            except CommandRejected as error:
                if error.code != "card_busy" or delay == 15:
                    raise

    @asynccontextmanager
    async def _card_display_batch(self, client, *, enabled=True):
        if not enabled or client.channel.capabilities.get("card_batch_display") != 1:
            yield
            return
        batch = secrets.token_hex(8)
        stopped = asyncio.Event()

        async def renew():
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(stopped.wait(), 8)
                    return
                except TimeoutError:
                    current = self.client
                    if current is not None:
                        with suppress(*LINK_ERRORS):
                            await current.set_card_batch(batch, opened=True)

        keeper = None
        try:
            await client.set_card_batch(batch, opened=True)
            keeper = asyncio.create_task(renew())
            yield
        finally:
            if keeper:
                # Cancelling an in-flight command invalidates its connection.
                # Let a renewal finish before releasing the lease or activating.
                stopped.set()
                await keeper
            if self.client is not None:
                with suppress(*LINK_ERRORS):
                    await self.client.set_card_batch(batch, opened=False)

    async def _install_role_bundle(self, bundle):
        async with self.storage_lock:
            client = self.client
            if client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            capabilities = client.channel.capabilities
            if any(capabilities.get(name) != 1 for name in ("card_transfer", "role_actions")):
                return {"status": "unsupported"}
            missing = inspect_role_requirements(bundle).missing(capabilities)
            if missing:
                flag = missing[0]
                return {"status": "unsupported", "reason": flag if flag == "role1_required" else flag + "_not_supported"}
            async with self._card_display_batch(client):
                completed = []
                try:
                    for item in bundle.files:
                        destination = f"roles/{bundle.identity}/{bundle.version}/{item.relative}"
                        client, transfer = await self._upload_card_file(
                            client, destination, item.source, expected_sha256=item.sha256)
                        completed.append(transfer)
                except LINK_ERRORS as error:
                    await self._drop_failed_upload(client)
                    return {"status": "transfer_failed", "completed": completed, "reason": str(error)}
                async with self.lock:
                    if self.client is not client:
                        return {"status": "activation_unconfirmed", "role": bundle.identity, "reason": "connection_changed"}
                    try:
                        try:
                            await client.prepare_role(bundle.identity, bundle.version, bundle.actions, bundle.finish_action)
                        except CommandRejected as error:
                            result = {"status": "activation_failed", "role": bundle.identity,
                                      "stage": "prepare", "reason": str(error)}
                        else:
                            result = await self.wait_role(bundle.identity)
                    except LINK_ERRORS as error:
                        if self.client is not None and not self.client.channel.online:
                            await self.drop()
                            self.snapshot = {"connection": "offline"}
                        return {"status": "activation_unconfirmed", "role": bundle.identity, "reason": str(error)}
                result.update(version=bundle.version, files=len(completed))
                return result

    async def select_role(self, identity):
        async with self.storage_lock, self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            await self.client.select_role(identity)
            return await self.wait_role(identity)

    async def role_preview(self, mode, identity):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("role_preview") != 1:
                return {"status": "unsupported"}
            reply = await self.client.role_preview(mode, identity)
            if reply["preview_status"] == "selected" and reply.get("role_state") == "loading":
                activation = await self.wait_role(reply["id"])
                if activation["status"] != "ready":
                    return activation
            result = {"status": reply["preview_status"]}
            if "id" in reply:
                result["role"] = reply["id"]
            if "stage" in reply:
                result["stage"] = reply["stage"]
            return result

    async def list_roles(self):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            return {"status": "ok", "roles": await self.client.list_roles()}

    async def list_actions(self):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            return {"status": "ok", "actions": await self.client.list_actions()}

    async def play_role(self, action, duration_ms):
        if duration_ms is not None and not 100 <= duration_ms <= 6000:
            raise ValueError("动作时长须为 100 至 6000 毫秒。")
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            actions = await self.client.list_actions()
            if action not in actions:
                return {"action": action, "status": "unsupported", "available_actions": actions}
            result = await self.client.play_role(actions.index(action), duration_ms or 0)
            reply = {"action": action, "status": result.get("action_status", "unknown")}
            if isinstance(result.get("reason"), str) and result["reason"]:
                reply["reason"] = result["reason"]
            if "operation" in result:
                reply.update(operation=result["operation"], boot=result["boot"])
            return reply

    async def role_action_status(self, operation, boot):
        if not 0 < operation <= 0xFFFFFFFF or not 0 <= boot <= 0xFFFFFFFF:
            raise ValueError("请提供动作启动回执中的 operation 和 boot。")
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("role_action_receipt") != 1:
                return {"status": "unsupported"}
            reply = await self.client.role_action_status(operation, boot)
            return {"status": reply["action_status"], "operation": operation, "boot": boot}

    async def set_role_base(self, action):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("role_base") != 1:
                return {"status": "unsupported", "reason": "device_does_not_support_base"}
            actions = await self.client.list_actions()
            if action not in actions:
                return {"action": action, "status": "unsupported", "available_actions": actions}
            result = await self.client.set_role_base(actions.index(action))
            return {"action": action, "status": result.get("base_status", "unknown")}

    async def wait_touch(self, timeout):
        if not 0 < timeout <= 60:
            raise ValueError("等待时间须大于 0 秒且不超过 60 秒。")
        try:
            event = await asyncio.wait_for(self.touches.get(), timeout)
        except asyncio.TimeoutError:
            return {"status": "timeout", "connection": self.snapshot}
        dropped = self.dropped_touches
        if self.client is not None:
            dropped += getattr(self.client.channel, "dropped_events", 0)
        return {"status": "received", "event": event, "dropped_events": dropped}

    async def image(self, source=None):
        async with self.storage_lock, self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            return await self.client.clear_image() if source is None else await self.client.show_image(source)

    async def add_theme(self,source,name):
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,23}',name):
            raise ValueError('主题名称请使用 1 至 24 位英文字母、数字、下划线或连字符，并以字母或数字开头。')
        path=Path(source).expanduser()
        if not path.is_absolute() or not path.is_file():raise ValueError('请提供本机主题 JSON 文件的绝对路径。')
        client=self.client
        if client is None:return {'status':'not_connected','connection':self.snapshot}
        if client.channel.capabilities.get('theme_json_upload')!=1:return {'status':'unsupported'}
        def read():
            with path.open('rb') as stream:return stream.read(2049)
        payload=await asyncio.to_thread(read)
        if not 0<len(payload)<=2048:raise ValueError('主题文件须为 1 至 2048 字节。')
        filename=f'{name}-{hashlib.sha256(payload).hexdigest()[:16]}.json'
        with tempfile.TemporaryDirectory(prefix='deskpet-theme-') as directory:
            staged=Path(directory)/'theme.json';staged.write_bytes(payload)
            item=CardFile(source=str(staged),path=f'themes/{filename}')
            return {'theme':filename,**await self.upload_files([item],expected_client=client)}

    async def add_wallpaper(self, source, name):
        path=Path(source).expanduser()
        if not path.is_absolute() or not path.is_file():raise ValueError('请提供本机图片的绝对路径。')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,23}',name):
            raise ValueError('壁纸名称须为 1 至 24 位英文字母、数字、下划线或连字符。')
        client=self.client
        if client is None:return {'status':'not_connected','connection':self.snapshot}
        if client.channel.capabilities.get('wallpaper_jpeg_upload')!=1:return {'status':'unsupported'}
        payload=await asyncio.to_thread(prepare_jpeg_photo,path)
        filename=f'{name}-{hashlib.sha256(payload).hexdigest()[:16]}.jpg'
        with tempfile.TemporaryDirectory(prefix='deskpet-wallpaper-') as directory:
            staged=Path(directory)/'wallpaper.jpg';staged.write_bytes(payload)
            relative=f'wallpapers/{filename}'
            return {'wallpaper':relative,**await self.upload_files([CardFile(source=str(staged),path=relative)],expected_client=client)}

    async def delete_wallpaper(self, path):
        async with self.storage_lock, self.lock:
            if self.client is None:return {'status':'not_connected','connection':self.snapshot}
            if self.client.channel.capabilities.get('wallpaper_delete')!=1:return {'status':'unsupported'}
            result=await self.client.delete_wallpaper(path)
            if type(result.get('deleted')) is not bool:raise DeviceError('invalid_wallpaper_delete_response')
            return {'path':path,'deleted':result['deleted']}

    async def list_wallpapers(self, cursor):
        async with self.storage_lock, self.lock:
            if self.client is None:return {'status':'not_connected','connection':self.snapshot}
            if self.client.channel.capabilities.get('wallpaper_list')!=1:return {'status':'unsupported'}
            return await self.client.list_wallpapers(cursor)

    async def music_control(self, action, path):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("music_control") != 1:
                return {"status": "unsupported"}
            try:
                return {"status": "ok", **await self.client.music_control(action, path=path)}
            except (AckTimeout, OSError):
                return {"status": "unknown", "reason": "music_ack_lost"}
            except CommandRejected as error:
                return {"status": "rejected", "reason": error.code}

    async def music_library(self, action, path, source):
        client = self.client
        if client is None:
            return {"status": "not_connected", "connection": self.snapshot}
        if client.channel.capabilities.get("music_library") != 1:
            return {"status": "unsupported"}
        if action == "import" and source:
            destination = path if path is not None else f"music/{Path(source).name}"
            validate_music_path(destination)
            return await self.upload_files([CardFile(source=source, path=destination)], expected_client=client)
        if action == "list" and path is None and source is None:
            async with self.lock:
                if self.client is not client:
                    return {"status": "connection_changed"}
                return {"status": "ok", "items": [{"path": name} for name in await client.list_music()]}
        if action == "delete" and path is not None and source is None:
            async with self.storage_lock, self.lock:
                if self.client is not client:
                    return {"status": "connection_changed"}
                try:
                    return {"status": "ok", "path": path, "deleted": await client.delete_music(path)}
                except (AckTimeout, OSError):
                    return {"status": "unknown", "reason": "music_ack_lost"}
                except CommandRejected as error:
                    return {"status": "rejected", "reason": error.code}
        raise ValueError("导入须提供本机绝对路径，删除须提供曲库返回的歌曲路径。")

    async def add_photo(self, source):
        path = Path(source).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("请提供本机图片的绝对路径。")
        client=self.client
        if client is not None and client.channel.capabilities.get('photo_jpeg_upload')==1:
            payload=await asyncio.to_thread(prepare_jpeg_photo,path)
            identity='p-'+hashlib.sha256(payload).hexdigest()[:16]
            with tempfile.TemporaryDirectory(prefix='deskpet-photo-') as directory:
                staged=Path(directory)/'photo.jpg';staged.write_bytes(payload)
                item=CardFile(source=str(staged),path=f'photos/{identity}.jpg')
                return {'photo':identity,**await self.upload_files([item],expected_client=client)}
        fullscreen = (self.client is not None
                      and self.client.channel.capabilities.get("photo_fullscreen") == 1)
        if fullscreen:
            full, thumb, screen = await asyncio.to_thread(prepare_photo_with_fullscreen, path)
            renditions = (("screen.565", screen), ("full.565", full), ("thumb.565", thumb))
        else:
            full, thumb = await asyncio.to_thread(prepare_photo, path)
            renditions = (("full.565", full), ("thumb.565", thumb))
        identity = "p-" + hashlib.sha256(full + thumb).hexdigest()[:16]
        with tempfile.TemporaryDirectory(prefix="deskpet-photo-") as directory:
            items = []
            for name, payload in renditions:
                staged = Path(directory) / name
                staged.write_bytes(payload)
                items.append(CardFile(source=str(staged), path=f"photos/{identity}/{name}"))
            return {"photo": identity, **await self.upload_files(items)}

    async def photo_album(self, mode, photo, interval_seconds):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            if self.client.channel.capabilities.get("photo_album") != 1:
                return {"status": "unsupported"}
            reply = await self.client.photo_album(mode, photo=photo, interval_seconds=interval_seconds)
            fields = ("view", "interval_seconds", "playing", "count", "page", "pages",
                      "photo", "photos", "thumbnails_drawn", "photo_drawn", "photo_draws")
            return {"status": "ok", **{key: reply[key] for key in fields if key in reply}}

    async def set_brightness(self, level):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            return {"status": "saved", "brightness": await self.client.set_brightness(level)}

    async def calibrate_touch(self, cancel=False):
        async with self.lock:
            if self.client is None:
                return {"status": "not_connected", "connection": self.snapshot}
            return await self.client.calibrate_touch(cancel)


def speed_windows_view(reply):
    value = reply.get("speed_windows")
    if not isinstance(value, dict):
        return None
    try:
        return asdict(SpeedConfig(value["recent_seconds"], value["long_days"]))
    except (KeyError, ValueError):
        return None


def battery_view(value):
    if not isinstance(value, dict):
        value = {}
    power = value.get("power")
    mv = value.get("mv")
    percent = value.get("percent")
    if power not in ("battery", "charging", "external_full"):
        power = "unknown"
    valid_mv = type(mv) is int and 0 < mv <= 6000
    valid_percent = type(percent) is int and 0 <= percent <= 100 and valid_mv and power == "battery"
    return {
        "mv": mv if valid_mv else None,
        "percent": percent if valid_percent else None,
        "power": power,
        "low": valid_mv and power == "battery" and value.get("low") is True,
    }


def status_view(reply, capabilities):
    def page_name(key):
        index = reply.get(key)
        return SCREEN_PAGES[index] if type(index) is int and 0 <= index < len(SCREEN_PAGES) else None

    status = {
        "connection": "connected",
        "protocol": capabilities.get("protocol"),
        "text_supported": capabilities.get("text_present") == 1,
        "page": reply.get("pet_page"),
        "screen_page": page_name("screen_page"),
        "home_page": page_name("home_page"),
        "clock_face": reply.get("clock_face"),
        "automatic_role": reply.get("automatic_role"),
        "boot": reply.get("boot"),
        "reset_reason": reply.get("reset_reason"),
        "text_status": reply.get("text_status"),
        "image_displayed": reply.get("image_displayed", False),
        "brightness": reply.get("brightness"),
        "touch_calibration": reply.get("touch_calibration", "unsupported"),
        "role": {"state": reply.get("role_state", "unsupported"), "id": reply.get("role_id"),
                 "action_count": reply.get("role_action_count", 0),
                 "completion_action": reply.get("finish_action") or None,
                 "remembered_after_restart": reply.get("role_persisted")},
    }
    if isinstance(reply.get("role_base_action"), str):
        status["role"]["base_action"] = reply["role_base_action"]
    if isinstance(reply.get("role_failure_stage"), str):
        status["role"]["failure_stage"] = reply["role_failure_stage"]
    if type(reply.get("weekly_reset")) is int and 1700000000 <= reply["weekly_reset"] <= 0xFFFFFFFF:
        status["weekly_reset_at"] = reply["weekly_reset"]
    quota_source = reply.get("quota_source")
    if capabilities.get("quota_sources") == 1 and type(quota_source) is int and 0 <= quota_source < len(SOURCES):
        status["quota_source"] = SOURCES[quota_source]
    if capabilities.get("speed_windows") == 1 and (windows := speed_windows_view(reply)) is not None:
        status["speed_windows"] = windows
    if capabilities.get("battery_status") == 1 and isinstance(reply.get("battery"), dict):
        status["battery"] = battery_view(reply["battery"])
    if capabilities.get("weather_device") == 1:
        status["weather"] = {"updated": reply.get("weather_updated") or None,
                             "device_refresh": reply.get("weather_device_refresh") is True}
        for key, field in (("cached", "weather_cached"), ("cached_on_screen", "weather_cache_indicator")):
            if type(reply.get(field)) is bool:
                status["weather"][key] = reply[field]
    if reply.get("transport") in ("usb", "wifi"):
        status["transport"] = reply["transport"]
    return status


def create_server(config_path, role_dir=None, token_root=None, usb_port=None, token_collector=None, token_probe=None,
                  *, no_token_probe=False, shared_config_path=None):
    if no_token_probe and (token_probe is not None or token_collector is not None):
        raise ValueError("禁用 Token 采集时不能同时指定采集程序。")
    shared_config = Path(shared_config_path).expanduser() if shared_config_path is not None else Path(config_path)
    if shared_config_path is not None:
        DeviceConfig.load(shared_config)
    if not no_token_probe and token_probe is None and token_collector is None:
        token_probe = find_installed_probe(shared_config)
    shared_probe = AgentSpeedProbe(token_probe) if token_probe is not None else None
    shared_quota_store = QuotaStore(shared_config.parent / "agent-quota.sqlite3")
    notification_inbox = NotificationStore(shared_config.parent / "notifications.sqlite3")
    default_source = notification_inbox if Path(config_path).resolve()!=shared_config.resolve() else None
    session = BoardSession(config_path, token_root=token_root, usb_port=usb_port,
                           token_collector=token_collector, probe_collector=shared_probe, no_token_probe=no_token_probe,
                           notification_source=default_source,quota_store=shared_quota_store)
    registry = DeviceRegistry(config_path)
    session.identity_registry = registry
    tasks = []
    running = False
    hook_configuration_lock = asyncio.Lock()

    def start_session(board):
        if running:
            board.loop = asyncio.get_running_loop()
            tasks.extend(asyncio.create_task(run()) for run in
                         (board.poll, board.poll_weather, board.poll_tokens, board.poll_quota,
                          board.poll_quota_display, board.poll_antigravity_quota, board.poll_computer_metrics, board.poll_analytics))

    def make_session(path):
        board = BoardSession(path, token_root=token_root, token_collector=token_collector, probe_collector=shared_probe,
                             no_token_probe=no_token_probe, notification_source=notification_inbox,
                             quota_store=shared_quota_store)
        board.identity_registry = registry
        return board

    def notification_commands(device_id, hook, *arguments):
        # Keep accepting a device selector from older clients. The hook is
        # installed once for this registry, and each board keeps its own inbox.
        if device_id is not None:
            registry.path_for(device_id)
        command = hook_command(shared_config, hook, *arguments)
        previous = [hook_command(path, hook, *arguments) for path in registry.paths()
                    if path.resolve() != shared_config.resolve()]
        return command, previous

    async def configure_notifications(enabled, device_id, hook, configure, *arguments):
        require_notification_hooks(enabled, os.name, hook)
        command, previous = notification_commands(device_id, hook, *arguments)

        async with hook_configuration_lock:
            return await asyncio.to_thread(configure, command, previous, enabled=enabled)

    sessions = DeviceSessions(registry, session, make_session, start_session)
    portal = None
    portal_mode = None
    portal_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_server):
        nonlocal running
        running = True
        try:
            start_session(session)
            for device in registry.list():
                sessions.resolve(device['id'])
            yield session
        finally:
            running = False
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for board in sessions.sessions.values():
                    await board.drop()
                if shared_probe is not None:
                    await shared_probe.close()
                if portal is not None:
                    await asyncio.to_thread(portal.close)
                tasks.clear()

    server = FastMCP(
        "DeskPet Platform", lifespan=lifespan,
        instructions="首次设置托肯时，调用 desk_notification_setup 查看 Codex、Claude Code、Antigravity、"
                     "OpenCode、WorkBuddy 和 WorkBuddy AI 的提醒支持、配置状态和启用工具。"
                     "向用户说明可以为日常使用的客户端启用提醒，按用户选择配置。"
                     "配置 Agent 额度后，按工具返回的 notification_setup 提示说明对应的提醒接入。"
                     "用户想给托肯添加、制作或安装角色时，先读 desk_read_manual(topic=roles)。"
                     "从描述或参考图生成全新角色动画需当前 Agent 具备生图能力（或由用户提供序列帧素材/使用代码绘制），"
                     "制作时按指南先用 desk_check_role 导出 320x240 首页预览供用户确认，再补齐动作与安装验收。"
                     "也可在电脑上生成内置 Pocket Bot 体验角色包，或将已有角色包安装到指定托肯的储存卡。",
    )

    async def add_pairing(config, registration=None):
        board = session.usb_client(config, registration) if registration else Client(config)
        async with board:
            await board.status()
            if board.device_id is not None:
                config = replace(config, device_id=board.device_id)
        identity = registry.add(config)
        added = sessions.resolve(identity)
        if registration is not None:
            added.usb_port = registration
        return {"device": config.identity, "id": identity, "device_id": config.device_id}

    def add_pairing_from_portal(code, address):
        future = asyncio.run_coroutine_threadsafe(add_pairing(DeviceConfig.from_code(code, address)), session.loop)
        try:
            return future.result(timeout=30)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("配对超时，请重试。") from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def desk_list_devices() -> dict[str, Any]:
        """列出这台电脑已配对的设备。多台设备时，操作须传入对应的 id。"""
        return {"devices": registry.list()}

    @server.tool()
    async def desk_find_usb_device(ports: list[str], device_id: str | None = None) -> dict[str, Any]:
        """读取指定 USB 串口上托肯设备的持久 UUID 和 MAC，可按 UUID 查当前串口。查询无需配对，不接管控制。"""
        return await asyncio.to_thread(find_usb_devices, ports, device_id)

    @server.tool()
    async def desk_set_usb_fallback(port: str, device_id: str) -> dict[str, Any]:
        """为已配对的托肯更新本次 MCP 运行中的备用串口。传入持久 UUID 和查到的串口。保留当前 Wi-Fi 控制。"""
        if not valid_device_uuid(device_id):
            raise ValueError("请提供已配对设备的持久 UUID。")
        return await sessions.resolve(device_id).set_usb_fallback(port, device_id)

    @server.tool()
    async def desk_usb_reset(port: str, device_id: str) -> dict[str, Any]:
        """通过原生 ESP USB 串口应急复位指定小板。设备 UUID 必须已配对，USB 序列号须与 MAC 匹配。"""
        if not valid_device_uuid(device_id):
            raise ValueError("请提供已配对设备的持久 UUID。")
        return await sessions.resolve(device_id).reset_usb(port, device_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def desk_read_manual(topic: Manual = "operations") -> str:
        """读取操作手册、SDK 二次开发手册或自定义角色指南。制作、体验或安装角色请选择 roles。无需连接托肯。"""
        return read_manual(topic)

    @server.resource("deskpet://manual/operations")
    def operations_manual() -> str:
        """安装、配网、配对、操作与故障排查。"""
        return read_manual("operations")

    @server.resource("deskpet://manual/development")
    def development_manual() -> str:
        """SDK、设备协议、扩展方式和验证。"""
        return read_manual("development")

    @server.resource("deskpet://manual/roles", name="添加自定义角色", mime_type="text/markdown")
    def roles_manual() -> str:
        """给托肯添加角色的对话示例、可运行样例和制作步骤。"""
        return read_manual("roles")

    @server.tool()
    async def desk_status(device_id: str | None = None) -> dict[str, Any]:
        """查询连接、设备能力和屏幕状态。储存卡操作期间返回最近一次读数。"""
        session = sessions.resolve(device_id)
        return await session.get_status()

    @server.tool()
    async def desk_computer_metrics(cpu: bool | None = None, memory: bool | None = None,
                                    device_id: str | None = None) -> dict[str, Any]:
        """查看或设置电脑 CPU、内存使用率是否显示；各设备单独保存，默认关闭。"""
        session = sessions.resolve(device_id)
        return await session.configure_computer_metrics(cpu=cpu, memory=memory)

    @server.tool()
    async def desk_render_metrics(reset: bool = False, device_id: str | None = None) -> dict[str, Any]:
        """读取角色帧率、读卡、画面合成、刷屏、触摸轮询与回执耗时。reset 清空采样窗口。"""
        session = sessions.resolve(device_id)
        return await session.render_metrics(reset)

    @server.tool()
    async def desk_sync_clock(offset_minutes: int | None = None, device_id: str | None = None) -> dict[str, Any]:
        """用电脑时间校准小屏，省略偏移时使用电脑本地时区。"""
        session = sessions.resolve(device_id)
        return await session.sync_clock(offset_minutes)

    @server.tool()
    async def desk_clock_edit(
        mode: Literal["status", "start", "adjust", "save", "cancel"] = "status",
        field: Literal["year", "month", "day", "hour", "minute"] | None = None,
        step: Literal[-1, 1] | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """在时钟页编辑日期和时间。adjust 每次调一个字段，save 应用草稿，cancel 放弃修改。"""
        session = sessions.resolve(device_id)
        return await session.clock_edit(mode, field, step)

    @server.tool()
    async def desk_restart(device_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        """重启指定的已配对托肯。confirm 为 true 时请求屏上确认，confirm_pending 表示正在等点按。"""
        return await sessions.resolve(device_id).restart(confirm=confirm)

    @server.tool()
    async def desk_recover_subsystem(target: Literal["sd", "i2c", "audio", "wifi"],
                                     device_id: str | None = None) -> dict[str, Any]:
        """请求恢复卡、触摸总线、音频或 Wi-Fi。accepted 表示已受理，设备在系统页显示执行结果。"""
        return await sessions.resolve(device_id).recover_subsystem(target)

    @server.tool()
    async def desk_set_view(page: Literal["role", "home", "clock", "settings", "roles", "photos", "weather", "messages"],
                            make_default: bool = False, clock_face: int | None = None, device_id: str | None = None) -> dict[str, Any]:
        """打开指定页面，可保存默认首页，或选择时钟表盘 0 到 5。"""
        session = sessions.resolve(device_id)
        return await session.set_view(page, make_default, clock_face)

    @server.tool()
    async def desk_voice_names(user_name: str | None = None, device_name: str | None = None,
                               device_id: str | None = None) -> dict[str, Any]:
        """查看或修改保存在托肯上的用户和设备昵称。用户昵称可为空；昵称不会改变唤醒词。"""
        return await sessions.resolve(device_id).voice_names(user_name=user_name,device_name=device_name)

    @server.tool()
    async def desk_voice_tools(enabled: list[Literal['device_status', 'list_role_actions', 'play_role_action',
                                                     'open_page', 'battery_status', 'weather_status', 'quota_status',
                                                     'device_settings', 'device_restart', 'music_control',
                                                     'photo_slideshow']] | None = None,
                               device_id: str | None = None) -> dict[str, Any]:
        """查看语音助手工具和确认要求，或用完整 enabled 列表替换授权。空列表撤销全部。"""
        return await sessions.resolve(device_id).voice_tool_settings(enabled)

    @server.tool()
    async def desk_agent_config(config: dict[str, Any] | None = None,
                                device_id: str | None = None) -> dict[str, Any]:
        """查询板载助手模式、令牌配置状态和最后已应用的交易，或提交用户明确提供的配置补丁。

        补丁可包含 mode、chat、asr、tts、persona、wake_enabled。令牌只写不读。
        """
        return await sessions.resolve(device_id).agent_config(config)

    @server.tool()
    async def desk_dashboard_config(
        metric: Literal['speed','quota','computer','auto','weather'] | None = None,
        quota_source: Literal['codex','claude_code','antigravity_gemini','antigravity_third_party'] | None = None,
        device_id: str | None = None,
        rotation: list[Literal['speed','quota','computer','weather']] | None = None,
        interval_seconds: Literal[5,10,20,30,60] | None = None,
    ) -> dict[str, Any]:
        """查看或分别设置数据项目、额度来源、轮播项目和间隔；项目至少选一项，设置在设备重启后保留。"""
        return await sessions.resolve(device_id).dashboard_config(metric,quota_source,rotation,interval_seconds)

    @server.tool()
    async def desk_appearance_config(theme: str | None = None, wallpaper: str | None = None,
                                     device_id: str | None = None) -> dict[str, Any]:
        """查看或单独设置主题、壁纸；壁纸可用照片标识或 photos/、media/、wallpapers/ 路径，空字符串清除。"""
        return await sessions.resolve(device_id).appearance_config(theme,wallpaper)

    @server.tool()
    async def desk_add_wallpaper(source: str, name: str = 'wallpaper', device_id: str | None = None) -> dict[str, Any]:
        """导入独立壁纸，不加入照片相框；名称可用英文字母、数字、下划线或连字符。"""
        return await sessions.resolve(device_id).add_wallpaper(source,name)

    @server.tool()
    async def desk_delete_wallpaper(path: str, device_id: str | None = None) -> dict[str, Any]:
        """删除指定的已导入壁纸；正在使用的壁纸需先切换。"""
        return await sessions.resolve(device_id).delete_wallpaper(path)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def desk_list_wallpapers(cursor: str = '', device_id: str | None = None) -> dict[str, Any]:
        """分页列出可用壁纸及当前选择；传入上页返回的 next_cursor 查看下一页。"""
        return await sessions.resolve(device_id).list_wallpapers(cursor)

    @server.tool()
    async def desk_add_theme(source: str, name: str = 'theme', device_id: str | None = None) -> dict[str, Any]:
        """导入设备配色 JSON；名称可用 1 至 24 位英文字母、数字、下划线或连字符。设备校验可读性。"""
        return await sessions.resolve(device_id).add_theme(source,name)

    @server.tool()
    async def desk_music_control(
        action: Literal["status", "play", "pause", "stop", "prev", "next", "shuffle"] = "status",
        path: str | None = None, device_id: str | None = None,
    ) -> dict[str, Any]:
        """查询或控制音乐。play 可指定歌曲路径。shuffle 会保存新顺序，整份列表播完后停止。"""
        return await sessions.resolve(device_id).music_control(action, path)

    @server.tool()
    async def desk_music_library(
        action: Literal["list", "import", "delete"] = "list", path: str | None = None,
        source: str | None = None, device_id: str | None = None,
    ) -> dict[str, Any]:
        """按播放顺序列歌、从本机绝对路径导入 MP3 或 FLAC，或按列表返回的完整路径删除指定歌曲。"""
        return await sessions.resolve(device_id).music_library(action, path, source)

    @server.tool()
    async def desk_add_photo(source: str, device_id: str | None = None) -> dict[str, Any]:
        """将本机图片加入设备的离线相册，同时生成缩略图。"""
        session = sessions.resolve(device_id)
        return await session.add_photo(source)

    @server.tool()
    async def desk_photo_album(
        mode: Literal["status", "grid", "open", "previous", "next", "play", "pause", "interval"] = "status",
        photo: str | None = None,
        interval_seconds: int | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """grid 打开或刷新相册。open 传照片标识，previous/next 翻页或切图，play/pause 控制轮播，interval 保存间隔。"""
        session = sessions.resolve(device_id)
        return await session.photo_album(mode, photo, interval_seconds)

    @server.tool()
    async def desk_set_weather_location(latitude: float, longitude: float, label: str, device_id: str | None = None) -> dict[str, Any]:
        """设定天气地点和屏幕上的英文名称，并刷新七天预报。"""
        session = sessions.resolve(device_id)
        return await session.set_weather_location(latitude, longitude, label)

    @server.tool()
    async def desk_token_status(device_id: str | None = None) -> dict[str, Any]:
        """读取最近一次发送到小屏的 Token 速率、趋势、会话数和额度。"""
        session = sessions.resolve(device_id)
        return session.token_status()

    @server.tool()
    async def desk_speed_windows(recent_seconds: int | None = None, long_days: int | None = None, device_id: str | None = None) -> dict[str, Any]:
        """读取自适应档位的比较窗口，或同时保存近期秒数和长期天数。近期可选 120、300、600、1800、3600，长期可选 1、3、7。"""
        session = sessions.resolve(device_id)
        return await session.speed_windows(recent_seconds, long_days)

    @server.tool()
    async def desk_set_role_auto(enabled: bool, device_id: str | None = None) -> dict[str, Any]:
        """按 Token 速率自动选择角色档位，或保留手动选择。"""
        session = sessions.resolve(device_id)
        return await session.set_role_auto(enabled)

    @server.tool()
    async def desk_diagnostics(include_card: bool = True, device_id: str | None = None) -> dict[str, Any]:
        """读取当前连接、已保存网络、重启原因及储存卡空间。"""
        session = sessions.resolve(device_id)
        return await session.diagnostics(include_card)

    @server.tool()
    async def desk_select_network(ssid: str, device_id: str | None = None) -> dict[str, Any]:
        """切换到已保存的 Wi-Fi。小屏会重新连接，失败时会尝试其他已保存网络。"""
        session = sessions.resolve(device_id)
        return await session.select_network(ssid)

    @server.tool()
    async def desk_pair_begin(open_browser: bool = True, add_device: bool = False) -> dict[str, Any]:
        """打开本机配对页面。添加另一台设备时设置 add_device；配对码只输入网页。"""
        nonlocal portal, portal_mode
        async with portal_lock:
            if not add_device and len(registry.list()) > 1:
                raise ValueError("已有多台设备，请使用添加设备配对。")
            reusable = (portal is not None and portal_mode == add_device and portal.remaining_seconds() > 0
                        and portal.status()["state"] in ("waiting", "checking"))
            if not reusable:
                if portal is not None and not await asyncio.to_thread(portal.close, only_if_idle=True):
                    raise ValueError("正在验证托肯，请等待本次配对结束，再打开新的配对页面。")
                portal = PairPortal(add_pairing_from_portal if add_device else session.pair_from_portal).start()
                portal_mode = add_device
            opened = await asyncio.to_thread(webbrowser.open, portal.url) if open_browser else False
            return {"url": portal.url, "browser_opened": opened, "expires_in_seconds": portal.remaining_seconds()}

    @server.tool()
    async def desk_pair_usb(port: str, expected_identity: str, credentials_file: str | None = None,
                            add_device: bool = False, device_id: str | None = None) -> dict[str, Any]:
        """用已确认的 USB 串口配对。添加另一台设备时设置 add_device。"""
        if add_device:
            if device_id is not None:
                raise ValueError("添加设备时无需指定已配对设备的 ID。")
            if any(item["identity"] == expected_identity for item in registry.list()):
                raise ValueError("小板已配对，请选择它的设备 ID。")
            registration = UsbPort.register(port)
            registration.verify()
            config = await asyncio.to_thread(probe_usb, port, expected_identity, credentials_file)
            registration.verify()
            return await add_pairing(config, registration)
        return await sessions.resolve(device_id).pair_usb(port, expected_identity, credentials_file)

    @server.tool()
    def desk_pair_status() -> dict[str, Any]:
        """查询本机配对页面的状态，不返回配对码。"""
        return portal.status() if portal is not None else {"state": "not_started", "device": None}

    @server.tool()
    async def desk_show_text(text: str = "DeskPet Platform ready", device_id: str | None = None) -> dict[str, Any]:
        """在已连接设备上显示不超过两行的文字，返回设备的显示回执。"""
        session = sessions.resolve(device_id)
        return await session.show_text(text)

    @server.tool()
    async def desk_upload_files(files: list[CardFile], device_id: str | None = None) -> dict[str, Any]:
        """将普通文件传到托肯储存卡的 desk/ 或 photos/。source 是绝对路径。角色资源请用 desk_install_role。"""
        if any(item.path.startswith("roles/") for item in files):
            raise ValueError("请用 desk_install_role 校验并安装角色资源。")
        session = sessions.resolve(device_id)
        return await session.upload_files(files)

    @server.tool()
    async def desk_delete_resource(path: str, device_id: str | None = None) -> dict[str, Any]:
        """删除已指定的卡上资源。用 roles/<角色标识> 或 photos/<照片标识> 删除整项，也可删除 desk 下的单个文件。"""
        session = sessions.resolve(device_id)
        return await session.delete_resource(path)

    @server.tool()
    async def desk_list_role_sources(directory: str | None = None, device_id: str | None = None) -> dict[str, Any]:
        """查询电脑上的角色候选目录，无需连接托肯；可覆盖启动时配置的绝对路径，安装时会完整校验。"""
        client = None
        if device_id is not None or len(sessions.registry.list()) < 2:
            client = sessions.resolve(device_id).client
        capabilities = client.channel.capabilities.copy() if client is not None else None
        return await asyncio.to_thread(list_role_sources, directory if directory is not None else role_dir, capabilities)

    @server.tool()
    async def desk_create_role_example(destination: str) -> dict[str, str]:
        """在本机生成用于体验安装和图鉴播放的 Pocket Bot 技术样例。destination 为新的 ZIP 文件绝对路径。无需连接托肯。"""
        return await asyncio.to_thread(create_role_example, destination)

    @server.tool()
    async def desk_check_role(source: str, preview_dir: str | None = None) -> dict[str, Any]:
        """在电脑上检查角色资源、首页活动范围与多帧主体稳定性；可选输出 320x240 首页合成与互动预览。source 和 preview_dir 均为绝对路径。无需连接托肯。"""
        from .role_install import prepare_role
        from .role_layout import inspect_role_layout, render_role_home_previews
        from .role_staging import stage_role

        if preview_dir is not None and not Path(preview_dir).expanduser().is_absolute():
            return {"status": "invalid", "reason": "请提供用于输出预览图的本机目录绝对路径。"}
        layout = None
        previews = None
        with tempfile.TemporaryDirectory(prefix="deskpet-role-check-") as directory:
            try:
                staged = await asyncio.to_thread(stage_role, source, directory)
                bundle = await asyncio.to_thread(prepare_role, staged)
                if bundle.portable:
                    layout = await asyncio.to_thread(inspect_role_layout, staged)
                    if preview_dir is not None:
                        previews = await asyncio.to_thread(render_role_home_previews, staged, preview_dir)
            except ValueError as error:
                return {"status": "invalid", "reason": str(error)}
            except OSError:
                return {"status": "invalid", "reason": "无法读取或暂存角色资源，请检查本机文件权限和可用空间。"}
        result: dict[str, Any] = {
            "status": "valid",
            "role": bundle.identity,
            "version": bundle.version,
            "format": "role1" if bundle.portable else "PET1",
            "actions": list(bundle.actions),
            "files": [item.relative for item in bundle.files],
        }
        if layout is not None:
            result["layout"] = layout
        if previews is not None:
            result["previews"] = previews
        return result

    @server.tool()
    async def desk_install_role(source: str, device_id: str | None = None,
                                variant: str | None = None) -> dict[str, Any]:
        """先在电脑上校验角色包，成功后才传到托肯储存卡并启用。图鉴角色使用 role1 ZIP。可用 desk_check_role 单独检查。source 是绝对路径或已配置角色素材目录中的角色 ID；按 ID 安装时可指定 variant。"""
        session = sessions.resolve(device_id)
        if not Path(source).is_absolute() and valid_identity(source):
            if session.client is None:
                return {"status": "not_connected", "connection": session.snapshot}
            selected = await asyncio.to_thread(
                select_role_source, role_dir, source, session.client.channel.capabilities.copy(), variant)
            if selected["status"] != "ok":
                return selected
            source = selected["path"]
        elif variant is not None:
            return {"status": "invalid", "reason": "variant_requires_role_id"}
        return await session.install_role(source)

    @server.tool()
    async def desk_select_role(identity: str, device_id: str | None = None) -> dict[str, Any]:
        """切换到已安装在储存卡上的角色，identity 是安装时返回的角色标识。"""
        session = sessions.resolve(device_id)
        return await session.select_role(identity)

    @server.tool()
    async def desk_role_preview(mode: Literal["start", "status", "cancel", "use"] = "status",
                                identity: str | None = None, device_id: str | None = None) -> dict[str, Any]:
        """试播已安装角色。start 需要角色标识，status 查看阶段，cancel 返回图鉴，use 设为当前角色。"""
        session = sessions.resolve(device_id)
        return await session.role_preview(mode, identity)

    @server.tool()
    async def desk_list_roles(device_id: str | None = None) -> dict[str, Any]:
        """查询储存卡中已安装的角色。"""
        session = sessions.resolve(device_id)
        return await session.list_roles()

    @server.tool()
    async def desk_list_actions(device_id: str | None = None) -> dict[str, Any]:
        """查询当前角色实际支持的动作名称。"""
        session = sessions.resolve(device_id)
        return await session.list_actions()

    @server.tool()
    async def desk_play_action(action: str, duration_ms: int | None = None, device_id: str | None = None) -> dict[str, Any]:
        """播放当前角色的动作；action 使用 desk_list_actions 返回的名称。默认使用动作自身的时长。"""
        session = sessions.resolve(device_id)
        return await session.play_role(action, duration_ms)

    @server.tool()
    async def desk_action_status(operation: int, boot: int, device_id: str | None = None) -> dict[str, Any]:
        """用启动回执中的 operation 和 boot 查询动作是否还在播放、完成或中断。"""
        session = sessions.resolve(device_id)
        return await session.role_action_status(operation, boot)

    @server.tool()
    async def desk_set_role_base(action: str, device_id: str | None = None) -> dict[str, Any]:
        """切换当前角色的持续动作；临时动作结束后回到这个持续动作。"""
        session = sessions.resolve(device_id)
        return await session.set_role_base(action)

    @server.tool()
    async def desk_wait_touch(timeout: float = 15, device_id: str | None = None) -> dict[str, Any]:
        """等待或读取小屏点击事件，最多等待 60 秒。"""
        session = sessions.resolve(device_id)
        return await session.wait_touch(timeout)

    @server.tool()
    async def desk_show_image(source: str, device_id: str | None = None) -> dict[str, Any]:
        """显示电脑上的图片；source 是图片文件绝对路径，传输时会自动转换并写入储存卡。"""
        session = sessions.resolve(device_id)
        return await session.image(source)

    @server.tool()
    async def desk_clear_image(device_id: str | None = None) -> dict[str, Any]:
        """退出图片，返回角色或设备状态画面。"""
        session = sessions.resolve(device_id)
        return await session.image()

    @server.tool()
    async def desk_set_brightness(level: int, device_id: str | None = None) -> dict[str, Any]:
        """设置屏幕亮度并保存；level 为 0 至 100，0 会关闭背光，触摸可唤醒。"""
        session = sessions.resolve(device_id)
        return await session.set_brightness(level)

    @server.tool()
    async def desk_calibrate_touch(device_id: str | None = None) -> dict[str, Any]:
        """在小屏上开始触摸位置校准，按屏幕提示依次点击三个标记。"""
        session = sessions.resolve(device_id)
        return await session.calibrate_touch()

    @server.tool()
    async def desk_cancel_touch_calibration(device_id: str | None = None) -> dict[str, Any]:
        """取消触摸校准，保留此前保存的校准。"""
        session = sessions.resolve(device_id)
        return await session.calibrate_touch(cancel=True)

    def notification_setup(client):
        from .doctor import hook_state

        name, tool, arguments = NOTIFICATION_SETUP[client]
        state = hook_state(client, shared_config)
        result = {"client": client, "name": name, "status": state["status"]}
        if state["status"] in ("missing", "partial", "needs_reinstall", "disabled"):
            result["enable"] = {"tool": tool, "arguments": arguments}
        if state["status"] == "written":
            result["next_step"] = ("在 Codex 的 Hooks 里检查是否已信任该钩子，再用新会话验证屏幕提醒。"
                                   if client == "codex" else "用新会话验证实际屏幕提醒。")
        elif state["status"] == "unsupported":
            result["next_step"] = "当前系统尚不支持这个客户端的提醒接入。"
        elif state["status"] == "unavailable":
            result["next_step"] = "找不到 Windows PowerShell，请检查本机安装。"
        elif state["status"] == "invalid":
            result["next_step"] = "检查客户端的钩子配置后再重试。"
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def desk_notification_setup() -> dict[str, Any]:
        """首次设置或新增 Agent 时，查看每个客户端的提醒支持、配置状态和启用工具。"""
        return {"clients": await asyncio.to_thread(lambda: [notification_setup(client) for client in NOTIFICATION_SETUP])}

    @server.tool()
    async def desk_enable_codex_notifications(device_id: str | None = None) -> dict[str, Any]:
        """安装一个 Codex 钩子，为所有已配对设备启用完成和待应答提醒。"""
        require_notification_hooks(True, os.name, "codex-hook")
        path = codex_home() / "hooks.json"
        command, previous = notification_commands(device_id, "codex-hook")
        added = await asyncio.to_thread(configure_codex_hooks, path, command, previous, enabled=True)
        return {"status": "review_required" if added else "configured", "added": added, "config": str(path),
                "next_step": "在 Codex 的 /hooks 或设置中的 Hooks 里检查并信任新增条目，然后在新会话中验证。"}

    @server.tool()
    async def desk_disable_codex_notifications(device_id: str | None = None) -> dict[str, Any]:
        """停止新增 Codex 会话提醒；只移除本程序的 hook，保留其他 hooks 和已有提醒。"""
        path = codex_home() / "hooks.json"
        command, previous = notification_commands(device_id, "codex-hook")
        removed = await asyncio.to_thread(configure_codex_hooks, path, command, previous, enabled=False)
        return {"status": "disabled", "removed": removed}

    @server.tool()
    async def desk_set_claude_notifications(enabled: bool, device_id: str | None = None) -> dict[str, Any]:
        """为所有已配对设备启用或关闭 Claude Code 的完成和提问提醒。"""
        from .claude_feedback import MATCHERS, settings_path

        path = settings_path()
        require_notification_hooks(enabled, os.name, "claude-hook")
        command, previous = notification_commands(device_id, "claude-hook")
        async with hook_configuration_lock:
            changed = await asyncio.to_thread(configure_hooks, path, command, previous, enabled=enabled,
                                               matchers=MATCHERS, private=False)
        if enabled:
            return {"status": "configured", "added": changed, "config": str(path),
                    "next_step": "打开新的 Claude Code 会话验证提醒。"}
        return {"status": "disabled", "removed": changed}

    @server.tool()
    async def desk_set_antigravity_notifications(enabled: bool, device_id: str | None = None) -> dict[str, Any]:
        """为所有已配对设备启用或关闭 Antigravity 完成和提问提醒。"""
        from .antigravity_feedback import configure, settings_path

        path = settings_path()
        changed = await configure_notifications(enabled, device_id, "antigravity-hook",
            lambda command, previous, *, enabled: configure(path, command, enabled=enabled, previous_commands=previous))
        result = {"status": "configured" if enabled else "disabled", "changed": changed, "config": str(path)}
        if enabled:
            result["next_step"] = "打开新的 Antigravity 会话确认托肯收到真实提醒；若未加载再重启客户端。"
        return result

    @server.tool()
    async def desk_set_agent_quota(source: Literal["claude_code", "antigravity"], enabled: bool) -> dict[str, Any]:
        """启用或移除指定 Agent 的额度采集。启用后返回对应提醒钩子的配置建议。"""
        from .quota_statusline import configure, settings_path

        path = settings_path(source)
        async with hook_configuration_lock:
            changed = await asyncio.to_thread(configure, path, source, shared_quota_store.path, enabled=enabled)
            if source == "antigravity":
                refresh_changed = await asyncio.to_thread(shared_quota_store.set_antigravity_refresh, enabled)
                changed = changed or refresh_changed
        result = {"status": "configured" if enabled else "disabled", "changed": changed, "config": str(path),
                  "next_step": ("在托肯的监控设置中选择对应来源，打开该 Agent 会话并检查额度；"
                                "账号未提供额度或状态栏被覆盖时显示 --。" if enabled else
                                "已移除本程序的额度采集设置。")}
        if enabled:
            result["notification_setup"] = await asyncio.to_thread(notification_setup, source)
            if "enable" in result["notification_setup"]:
                result["next_step"] += " 该客户端也支持完成和待应答提醒，可按 notification_setup 启用。"
        return result

    @server.tool()
    async def desk_set_opencode_notifications(enabled: bool, device_id: str | None = None) -> dict[str, Any]:
        """为所有已配对设备启用或关闭 OpenCode 的完成、提问和授权提醒。"""
        from .opencode_feedback import configure_registry, plugin_path

        path = plugin_path(hook_command(shared_config, "opencode-hook"))
        changed = await configure_notifications(enabled, device_id, "opencode-hook", configure_registry)
        return {"status": "configured" if enabled else "disabled", "changed": changed, "config": str(path),
                "next_step": "重启 OpenCode 后验证提醒。"}

    @server.tool()
    async def desk_set_workbuddy_notifications(
        enabled: bool, edition: Literal["workbuddy", "workbuddy_ai"] = "workbuddy", device_id: str | None = None,
    ) -> dict[str, Any]:
        """为所有已配对设备启用或关闭 WorkBuddy 的完成、提问和授权提醒。"""
        from .workbuddy_feedback import MATCHERS, settings_path

        path = settings_path(edition)
        changed = await configure_notifications(enabled, device_id, "workbuddy-hook",
            lambda command, previous, *, enabled: configure_hooks(
                path, command, previous, enabled=enabled, matchers=MATCHERS, private=False),
            "--edition", edition)
        if enabled:
            return {"status": "configured", "added": changed, "config": str(path),
                    "next_step": "重启所选的 WorkBuddy 客户端，用新会话验证提醒。"}
        return {"status": "disabled", "removed": changed}

    @server.tool()
    async def desk_list_notifications(limit: int = 30, device_id: str | None = None) -> dict[str, Any]:
        """查看托肯自己的会话提醒，不读取或修改客户端的未读状态。"""
        session = sessions.resolve(device_id)
        await asyncio.to_thread(session.notifications.sync)
        items = await asyncio.to_thread(session.notifications.list, limit)
        return {"notifications": items, "unseen": await asyncio.to_thread(session.notifications.unseen_count)}

    @server.tool()
    async def desk_speak(text: str, device_id: str | None = None) -> dict[str, Any]:
        """主动朗读不超过 80 字的单行文字。文字会发送至微软 Edge 在线语音服务，音频不保存在设备卡上。"""
        return await sessions.resolve(device_id).speak(text)

    @server.tool()
    async def desk_read_notification(identity: int, device_id: str | None = None) -> dict[str, Any]:
        """主动朗读一条提醒的标题；标题会发送至微软 Edge 在线语音服务，不改变已读状态。"""
        return await sessions.resolve(device_id).read_notification(identity)

    @server.tool()
    async def desk_message_view(
        mode: Literal["status", "list", "open", "previous", "next", "mark_seen"] = "status",
        identity: int | None = None,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """用 list 打开屏幕提醒列表。标准固件在托肯屏幕上直接翻页和打开详情；旧版/参考固件支持远程翻页、查看和标记，open 和 mark_seen 需要通知编号。"""
        session = sessions.resolve(device_id)
        return await session.message_view(mode, identity)

    @server.tool()
    async def desk_mark_notification_seen(identity: int, device_id: str | None = None) -> dict[str, Any]:
        """只标记托肯的一条提醒为已查看，不改变客户端里的未读状态。"""
        session = sessions.resolve(device_id)
        return {"updated": await asyncio.to_thread(session.notifications.mark_seen, identity)}

    return server
