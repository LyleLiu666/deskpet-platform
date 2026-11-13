import asyncio
from pathlib import Path
import tempfile
import unittest

from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.channel import Channel, CommandRejected
from deskpet_platform.client import Client
from deskpet_platform.pairing import DeviceConfig
import json


class TouchTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_receipt_survives_touch_queue_overflow(self):
        class Wire:
            async def write(self, raw):
                request = json.loads(raw[3:])
                self.replies = iter([
                    {"type": "touch", "epoch": request["epoch"], "boot": 1, "operation": operation,
                     "version": 0, "id": "screen", "action": "tap"}
                    for operation in range(1, 19)
                ] + [{"epoch": request["epoch"], "seq": request["seq"], "boot": 1, "ok": True, "stored": True}])

            async def readline(self):
                return b"P1," + json.dumps(next(self.replies)).encode() + b"\n"

        channel = Channel(Wire(), "02:00:00:00:00:01")
        channel.epoch, channel.boot, channel.online = "a" * 32, 1, True
        self.assertTrue((await channel.send("card_fetch_status", operation=7))["stored"])
        self.assertTrue(channel.online)
        self.assertEqual(channel.dropped_events, 2)
        self.assertEqual([channel.events.get_nowait()["operation"] for _ in range(16)], list(range(3, 19)))

    async def test_expired_notice_detail_keeps_the_connection_and_delivers_the_touch(self):
        class BoardChannel:
            capabilities = {"touch_demo": 1}
            online = True

        class Board:
            channel = BoardChannel()

            def __init__(self):
                self.event = {"action": "notice_open", "id": "screen"}
                self.closed = False

            async def status(self):
                return {}

            async def pending_touch(self):
                event, self.event = self.event, None
                return event

            async def show_text(self, _text):
                return {"status": "shown"}

            async def show_notice_detail(self, _identity):
                raise CommandRejected("notice_not_visible")

            async def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = board = Board()
            session.notifications.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            task = asyncio.create_task(session.poll())
            try:
                result = await session.wait_touch(0.5)
                self.assertEqual(result["status"], "received")
                self.assertEqual(result["event"]["action"], "notice_open")
                self.assertIs(session.client, board)
                self.assertFalse(board.closed)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_late_touch_ack_on_old_firmware_delivers_event_and_keeps_connection(self):
        class OldFirmware:
            async def write(self, raw):
                request = json.loads(raw[3:])
                reply = {"epoch": request["epoch"], "seq": request["seq"], "boot": 1, "ok": True}
                if request["type"] == "touch_ack":
                    reply.update(ok=False, error="unsupported_command")
                self.reply = b"P1," + json.dumps(reply).encode() + b"\n"

            async def readline(self):
                return self.reply

        config = DeviceConfig("02:00:00:00:00:01", bytes(32))
        for receive in ("pending_touch", "next_touch"):
            with self.subTest(receive=receive):
                client = Client(config)
                client.channel = channel = Channel(OldFirmware(), config.identity)
                channel.epoch, channel.boot, channel.online = "a" * 32, 1, True
                channel.capabilities = {"touch_demo": 1}
                event = {"type": "touch", "epoch": channel.epoch, "boot": 1, "operation": 4,
                         "version": 0, "id": "screen", "action": "tap"}
                channel.capture_event(b"P1," + json.dumps(event).encode() + b"\n")
                self.assertEqual(await getattr(client, receive)(), event)
                self.assertIsNone(await client.pending_touch())
                self.assertTrue(channel.online)
                self.assertTrue((await client.status())["ok"])

    async def test_notification_touch_contains_only_valid_local_id(self):
        channel = Channel(None, "02:00:00:00:00:01")
        channel.epoch, channel.boot = "a" * 32, 1
        event = {"type": "touch", "epoch": channel.epoch, "boot": 1, "operation": 4,
                 "version": 0, "id": "screen", "action": "notice_seen",
                 "notification_id": 12, "x": 120, "y": 150}
        self.assertTrue(channel.capture_event(b"P1," + json.dumps(event).encode() + b"\n"))
        self.assertEqual((await channel.events.get())["notification_id"], 12)
    async def test_polling_acknowledges_and_delivers_a_touch_to_mcp(self):
        class Channel:
            capabilities = {"touch_demo": 1}
            dropped_events = 2
        class Board:
            channel = Channel()
            def __init__(self):
                self.event = {"action": "tap", "id": "screen", "x": 125, "y": 64}
            async def status(self):
                return {}
            async def pending_touch(self):
                event, self.event = self.event, None
                return event

        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = Board()
            session.dropped_touches = 1
            task = asyncio.create_task(session.poll())
            try:
                result = await session.wait_touch(2)
                self.assertEqual(result["status"], "received")
                self.assertEqual(result["event"]["id"], "screen")
                self.assertEqual(result["event"]["action"], "tap")
                self.assertEqual((result["event"]["x"], result["event"]["y"]), (125, 64))
                self.assertEqual(result["dropped_events"], 3)
                self.assertEqual((await session.wait_touch(0.01))["status"], "timeout")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_screen_notice_only_updates_its_own_seen_state(self):
        class Board:
            def __init__(self):
                self.detail = None
            async def show_text(self, text):
                self.text = text
                return {"status": "shown"}
            async def show_notice_detail(self, identity):
                self.detail = identity
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = board = Board()
            identity = session.notifications.add_codex_event(
                {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t", "cwd": "/tmp/project"})
            await session.process_touch({"id": "screen", "action": "notice_open", "x": 250, "y": 10})
            self.assertEqual(board.detail, identity)
            await session.process_touch({"id": "screen", "action": "notice_seen", "x": 120, "y": 150,
                                         "notification_id": identity})
            self.assertEqual(session.notifications.unseen_count(), 0)
