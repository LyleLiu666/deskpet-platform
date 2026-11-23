import asyncio
import io
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

from PIL import Image

from deskpet_platform.channel import AckTimeout, CommandRejected, DeviceError
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession, status_view
from deskpet_platform.role_example import create_role_example
from deskpet_platform.role_sources import select_role_source


SMALL = {"card_transfer": 1, "role_actions": 1, "role_zip_motion": 1, "role1_required": 1}
LARGE = dict(SMALL, role_photo_motion=1, role_video_timeline=1, role_burst_audio=1)


class MultiBoardContractTests(unittest.IsolatedAsyncioTestCase):
    def test_battery_status_keeps_unknown_inputs_and_avoids_false_low_state(self):
        capability = {"battery_status": 1}
        reply = {"battery": {"mv": 4100, "percent": 100, "power": "unknown", "low": True}}
        self.assertNotIn("battery", status_view(reply, {}))
        self.assertEqual(status_view(reply, capability)["battery"],
                         {"mv": 4100, "percent": None, "power": "unknown", "low": False})
        reply["battery"] = {"mv": True, "percent": False, "power": {"raw": "invalid"}, "low": 1}
        self.assertEqual(status_view(reply, capability)["battery"],
                         {"mv": None, "percent": None, "power": "unknown", "low": False})
        reply["battery"] = {"mv": 3400, "percent": 12, "power": "battery", "low": True}
        self.assertEqual(status_view(reply, capability)["battery"], reply["battery"])
    async def test_music_listing_restarts_after_a_playlist_version_change(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        send = AsyncMock(side_effect=[
            {"items": [{"path": "music/旧列表.mp3"}], "next_cursor": "old"},
            CommandRejected("stale_cursor"),
            {"items": [{"path": "media/晚风 🎵.FLAC"}], "next_cursor": "new"},
            {"items": [{"path": "music/晚风 🎵.FLAC"}], "next_cursor": None},
        ])
        client.channel = SimpleNamespace(capabilities={"music_library": 1}, send=send)
        self.assertEqual(await client.list_music(), ["media/晚风 🎵.FLAC", "music/晚风 🎵.FLAC"])
        self.assertEqual([call.kwargs.get("cursor") for call in send.await_args_list], [None, "old", None, "new"])

    async def test_music_control_preserves_the_wire_path_and_validates_input(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        path = "music/晚风 🎵.mp3"
        status = {"path": path, "title": "晚风", "state": "playing", "position_ms": 0,
                  "shuffled": True, "volume": 25}
        send = AsyncMock(return_value=status)
        client.channel = SimpleNamespace(capabilities={"music_control": 1}, send=send)
        with self.assertRaises(ValueError):
            await client.music_control("play", path="music/../song.mp3")
        with self.assertRaises(ValueError):
            await client.music_control("shuffle", path=path)
        send.assert_not_awaited()
        self.assertEqual(await client.music_control("play", path=path), status)
        send.assert_awaited_once_with("music_control", action="play", path=path)

    async def test_music_library_import_uses_the_restricted_card_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            path = Path(directory) / "晚风.mp3"
            path.write_bytes(b"audio")
            channel = SimpleNamespace(capabilities={"music_library": 1, "card_restricted_paths": 1,
                                                     "card_transfer": 1})
            session.client = SimpleNamespace(channel=channel)
            session._upload_card_file = AsyncMock(return_value=(session.client, {"stored": True}))
            await session.music_library("import", None, str(path))
            session._upload_card_file.assert_awaited_once_with(session.client, "music/晚风.mp3", path)
            session._upload_card_file.reset_mock()
            with self.assertRaises(ValueError):
                await session.music_library("import", "music/晚风.wav", str(path))
            session._upload_card_file.assert_not_awaited()
    async def test_restart_requires_capability_and_distinguishes_local_confirmation(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = SimpleNamespace(capabilities={}, send=AsyncMock())
        with self.assertRaisesRegex(DeviceError, "system_restart_not_supported"):
            await client.restart()
        client.channel.send.assert_not_awaited()
        client.channel.capabilities["system_restart"] = 1
        client.channel.send.return_value = {"status": "accepted", "boot": 12}
        self.assertEqual(await client.restart(), {"status": "accepted", "boot": 12})
        client.channel.send.assert_awaited_once_with("system_restart")
        client.channel.send.reset_mock()
        client.channel.send.return_value = {"status": "confirm_pending", "boot": 12}
        self.assertEqual(await client.restart(confirm=True), {"status": "confirm_pending", "boot": 12})
        client.channel.send.assert_awaited_once_with("system_restart", confirm=True)
        client.channel.send.reset_mock()
        with self.assertRaises(ValueError):
            await client.restart(confirm=True, capture_ticket=7)
        client.channel.send.assert_not_awaited()
        client.channel.capabilities["assistant_onboard"] = 1
        self.assertEqual(await client.restart(confirm=True, capture_ticket=7), {"status": "confirm_pending", "boot": 12})
        client.channel.send.assert_awaited_once_with("system_restart", confirm=True, ticket=7)
        client.channel.send.reset_mock()
        client.channel.capabilities["maintenance_restart_confirm"] = 1
        self.assertEqual(await client.restart(confirm=True), {"status": "confirm_pending", "boot": 12})
        client.channel.send.assert_awaited_once_with("maintenance_restart_confirm")
        client.channel.send.reset_mock()
        self.assertEqual(await client.restart(confirm=True, capture_ticket=7), {"status": "confirm_pending", "boot": 12})
        client.channel.send.assert_awaited_once_with("system_restart", confirm=True, ticket=7)

    async def test_voice_settings_send_only_temporary_ticketed_writes(self):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        send = AsyncMock(return_value={"volume": 34, "brightness": 85})
        client.channel = SimpleNamespace(capabilities={"device_settings": 1}, send=send)
        self.assertEqual(await client.voice_device_settings(), {"volume": 34, "brightness": 85})
        send.assert_awaited_once_with("device_settings")
        send.reset_mock()
        for kwargs in ({"volume": 34}, {"volume": True, "capture_ticket": 7}, {"brightness": 5, "capture_ticket": 7}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await client.voice_device_settings(**kwargs)
        send.assert_not_awaited()
        self.assertEqual(await client.voice_device_settings(volume=34, capture_ticket=7),
                         {"volume": 34, "brightness": 85})
        send.assert_awaited_once_with("device_settings", ticket=7, volume=34)
        send.return_value = {"volume": 34, "brightness": 85}
        with self.assertRaisesRegex(DeviceError, "invalid_device_settings_reply"):
            await client.voice_device_settings(brightness=65, capture_ticket=7)

    async def test_device_info_exposes_only_valid_diagnostic_summary(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        info = {"board": "synthetic", "chip": "esp32s3", "profile": "core",
                "partition_id": "synthetic-dual", "screen": [480, 320], "modules": ["core"]}
        summary = {"reset_reason": "watchdog", "early_resets": 3, "safe_mode": True,
                   "crash_present": True, "crash_pc": 0x42002000,
                   "last_subsystem": {"target": "wifi", "status": "reconnecting"}}
        client.channel = SimpleNamespace(capabilities={"device_info": 1}, send=AsyncMock())
        client.channel.send.return_value = dict(info, diagnostics=dict(
            summary, memory="synthetic", crash_lr=True,
            last_subsystem=dict(summary["last_subsystem"], password="synthetic")))
        self.assertEqual(await client.device_info(), dict(info, diagnostics=summary))
        client.channel.send.return_value = dict(info, diagnostics=dict(summary, early_resets=True))
        self.assertEqual(await client.device_info(), info)

    async def test_device_info_reports_pending_restart_write_categories(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        info = {"board": "synthetic", "chip": "esp32s3", "profile": "core",
                "partition_id": "synthetic-dual", "screen": [480, 320], "modules": ["core"]}
        summary = {"reset_reason": "software", "early_resets": 0,
                   "safe_mode": False, "crash_present": False}
        categories = ["card", "inbox", "merit"]
        client.channel = SimpleNamespace(capabilities={"device_info": 1}, send=AsyncMock())
        client.channel.send.return_value = dict(info, diagnostics=dict(summary, restart_pending_writes=categories))
        self.assertEqual((await client.device_info())["diagnostics"], dict(summary, restart_pending_writes=categories))

        for invalid in ([], "card", ["card", "card"], ["inbox", {"title": "private"}], ["merit", "unknown"]):
            with self.subTest(value=invalid):
                client.channel.send.return_value = dict(info, diagnostics=dict(summary, restart_pending_writes=invalid))
                self.assertEqual((await client.device_info())["diagnostics"], summary)

        summary["reset_reason"] = "brownout"
        client.channel.send.return_value = dict(info, diagnostics=dict(summary, restart_pending_writes=categories))
        self.assertEqual((await client.device_info())["diagnostics"], summary)

    async def test_device_info_reports_retained_low_battery_write_results(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        info = {"board": "synthetic", "chip": "esp32s3", "profile": "core",
                "partition_id": "synthetic-dual", "screen": [480, 320], "modules": ["core"]}
        summary = {"reset_reason": "software", "early_resets": 0,
                   "safe_mode": False, "crash_present": False}
        retained = {"level": 3, "test": True, "previous_boot": True, "pending": [],
                    "timed_out": ["card", "inbox"], "failed": ["card"], "upload": "cleanup_failed"}
        client.channel = SimpleNamespace(capabilities={"device_info": 1}, send=AsyncMock())
        client.channel.send.return_value = dict(info, diagnostics=dict(
            summary, low_battery_writes=dict(retained, path="synthetic")))
        self.assertEqual((await client.device_info())["diagnostics"]["low_battery_writes"], retained)
        for fields in ({"level": True}, {"previous_boot": "true"}, {"pending": None},
                       {"timed_out": ["card", "card"]}, {"failed": ["serial-token"]}):
            with self.subTest(fields=fields):
                client.channel.send.return_value = dict(
                    info, diagnostics=dict(summary, low_battery_writes=dict(retained, **fields)))
                self.assertEqual((await client.device_info())["diagnostics"], summary)
        client.channel.send.return_value = dict(info, diagnostics=dict(
            summary, low_battery_writes=dict(retained, upload="synthetic")))
        result = (await client.device_info())["diagnostics"]["low_battery_writes"]
        self.assertNotIn("upload", result)

    async def test_diagnostics_refreshes_safe_mode_without_requiring_card_support(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            summary = {"reset_reason": "watchdog", "early_resets": 3, "safe_mode": True,
                       "crash_present": False}
            summary["low_battery_writes"] = {"level": 2, "test": False, "previous_boot": True,
                                              "pending": ["merit"], "timed_out": [], "failed": []}
            session.client = SimpleNamespace(channel=SimpleNamespace(capabilities={"device_info": 1}),
                                             device_info=AsyncMock(return_value={"diagnostics": summary}))
            result = await session.diagnostics(include_card=True)
            self.assertEqual(result, {"status": "ok", "diagnostics": summary})
            self.assertEqual(session.device_info, (session.client, {"diagnostics": summary}))

    async def test_device_info_is_opt_in_and_checks_geometry(self):
        client = object.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = SimpleNamespace(capabilities={}, send=AsyncMock())
        self.assertIsNone(await client.device_info())
        client.channel.send.assert_not_awaited()
        client.channel.capabilities = {"device_info": 1}
        info = {"board": "synthetic-large", "chip": "esp32s3", "profile": "synthetic-video",
                "partition_id": "synthetic-dual-ota", "screen": [480, 320], "modules": ["core", "role_video"]}
        client.channel.send.return_value = dict(info, seq=1)
        self.assertEqual(await client.device_info(), info)
        client.channel.send.assert_awaited_once_with("device_info")
        client.channel.send.return_value = dict(info, screen=[480, True])
        with self.assertRaisesRegex(DeviceError, "invalid_device_info"):
            await client.device_info()

    async def test_status_survives_a_failed_device_info_query_and_caches_the_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            info = {"board": "synthetic-small", "chip": "esp32", "profile": "synthetic-core",
                    "partition_id": "synthetic-single", "screen": [320, 240], "modules": ["core"]}
            session.client = SimpleNamespace(
                channel=SimpleNamespace(capabilities={"device_info": 1}), status=AsyncMock(return_value={}),
                device_info=AsyncMock(side_effect=AckTimeout("ack_timeout: device_info; seq=2")), close=AsyncMock())
            status = await session.get_status()
            self.assertNotEqual(status["connection"], "offline")
            self.assertEqual(status["device_info_error"], "ack_timeout")
            self.assertIsNotNone(session.client)
            session.client.close.assert_not_awaited()
            session.client.device_info = AsyncMock(return_value=info)
            self.assertEqual((await session.get_status())["device_info"], info)
            self.assertEqual((await session.get_status())["device_info"], info)
            session.client.device_info.assert_awaited_once()

    async def test_selects_one_role_for_both_boards_and_rejects_before_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lite, video = root / "base.zip", root / "rich.zip"
            create_role_example(str(lite))
            with ZipFile(lite) as source, ZipFile(video, "w") as target:
                for name in source.namelist():
                    target.writestr(name, source.read(name))
                image = io.BytesIO()
                Image.new("RGB", (320, 480)).save(image, "JPEG")
                photo = image.getvalue()
                heads = struct.pack("<4s4H", b"RSC2", 480, 320, 19, 1)
                tracks = b"".join(struct.pack("<4H", index, 1, 50, 0) for index in range(19))
                refs = bytes(38)
                start = len(heads + tracks + refs) + 8
                target.writestr("scene-frames.bin", heads + tracks + refs +
                                struct.pack("<II", start, start + len(photo)) + photo)
                target.writestr("burst-audio.pcm", bytes(3200))

            identity = "user-pocket-bot"
            self.assertEqual(select_role_source(root, identity, SMALL)["path"], str(lite))
            self.assertEqual(select_role_source(root, identity, LARGE)["path"], str(video))
            self.assertEqual(select_role_source(root, identity, LARGE, "lite")["path"], str(lite))
            duplicate = root / "second-rich.zip"
            shutil.copyfile(video, duplicate)
            self.assertEqual(select_role_source(root, identity, LARGE)["reason"], "conflicting_role_variants")
            duplicate.unlink()
            blocked = select_role_source(root, identity, SMALL, "video-burst")
            self.assertEqual(blocked["reason"], "no_compatible_variant")
            self.assertIn("role_burst_audio", blocked["missing_capabilities"])
            lite.unlink()
            self.assertEqual(select_role_source(root, identity, SMALL)["reason"], "no_compatible_variant")

            session = BoardSession(root / "device.json")
            session.client = SimpleNamespace(channel=SimpleNamespace(capabilities=SMALL))
            session._upload_card_file = AsyncMock()
            rejected = await session.install_role(str(video))
            self.assertEqual(rejected["reason"], "role_photo_motion_not_supported")
            session._upload_card_file.assert_not_awaited()
            session.client.channel.capabilities = dict(LARGE, role_burst_audio=0)
            rejected = await session.install_role(str(video))
            self.assertEqual(rejected["reason"], "role_burst_audio_not_supported")
            session._upload_card_file.assert_not_awaited()
