import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.channel import Channel, CommandRejected
from deskpet_platform.client import Client
from deskpet_platform.pairing import DeviceConfig
import json


class TouchTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_message_opens_only_the_matching_pending_session(self):
        class Wire:
            def __init__(self):
                self.requests = []
                self.pending = None

            async def write(self, raw):
                self.request = json.loads(raw[3:])
                self.requests.append(self.request)

            async def readline(self):
                reply = {k: self.request[k] for k in ("epoch", "seq")}
                reply.update(ok=True, boot=1)
                if self.request["type"] == "notice_pending":
                    reply["ids"] = []
                if self.request["type"] == "notice_session_open" and "rows" in self.request and self.pending:
                    reply["request"] = self.pending
                return b"P1," + json.dumps(reply).encode() + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            board = BoardSession(Path(directory) / "device.json")
            config = DeviceConfig("02:00:00:00:00:01", bytes(32))
            client = board.client = Client(config)
            channel = client.channel = Channel(wire := Wire(), config.identity)
            channel.epoch, channel.boot, channel.online = "a" * 32, 1, True
            channel.capabilities = {"message_card_summary": 1, "notice_session_open": 1}
            thread = "019a2da9-e5d0-7000-844a-1c30b7f5477d"
            notifications = board.notifications
            old = notifications.add_agent_event("codex", thread, "old", "response", "old")
            active = notifications.add_agent_event("codex", thread, "new", "response", "current")
            other = notifications.add_agent_event("antigravity", thread, "ask", "response", "other")
            created = {item["id"]: int(item["created_at"]) for item in notifications.list()}
            with (patch("deskpet_platform.desktop_launch.sys.platform", "darwin"),
                  patch("deskpet_platform.desktop_launch.subprocess.run") as launch):
                await board.refresh_messages()
                self.assertIn([[active, created[active]]],
                              [request.get("rows") for request in wire.requests])
                records, _ = notifications.legacy_message_snapshot(0, 0, include_session_open=True)
                for ticket, identity, when, expected in (
                    (1, active, created[active], True),
                    (2, active, created[active] + 1, False),
                    (3, old, created[old], False),
                    (4, other, created[other], False),
                ):
                    wire.pending = {"id": identity, "created": when, "ticket": ticket}
                    await board.refresh_message_session_open(client, records)
                    self.assertEqual(wire.requests[-2]["rows"], [[active, created[active]]])
                    self.assertEqual(wire.requests[-1]["ticket"], ticket)
                    self.assertEqual(wire.requests[-1]["accepted"], expected)
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", f"codex://threads/{thread}"])

                notifications.resolve_response(active)
                wire.pending = {"id": active, "created": created[active], "ticket": 5}
                await board.refresh_message_session_open(client, records)
                self.assertFalse(wire.requests[-1]["accepted"])
                launch.assert_called_once()

    async def test_desktop_launch_routes_only_matching_cached_notifications(self):
        class Wire:
            async def write(self, raw):
                self.request = json.loads(raw[3:])

            async def readline(self):
                return b"P1," + json.dumps({**self.request, "ok": True, "boot": 1}).encode() + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            config = DeviceConfig("02:00:00:00:00:01", bytes(32))
            client = session.client = Client(config)
            wire = Wire()
            channel = client.channel = Channel(wire, config.identity)
            channel.epoch, channel.boot, channel.online = "a" * 32, 1, True
            channel.capabilities = {"touch_demo": 1}
            thread = "019a2da9-e5d0-7000-844a-1c30b7f5477d"
            for source in ("codex", "antigravity", "claude_code"):
                session.notifications.add_agent_event(source, thread, "turn", "completed", source)
            records = {row["source"]: row["cache_id"] for row in session.notifications.cache_records()}
            with (patch("deskpet_platform.desktop_launch.sys.platform", "darwin"),
                  patch("deskpet_platform.desktop_launch.subprocess.run") as launch):
                for operation, (identity, status) in enumerate([
                    (records["codex"], "accepted"), (records["antigravity"], "accepted"),
                    (records["claude_code"], "rejected"),
                    (records["codex"] + "f", "rejected"),
                ], 1):
                    touch = {"type": "touch", "epoch": channel.epoch, "boot": 1, "operation": operation,
                             "version": 0, "id": identity, "action": "open_session"}
                    channel.capture_event(b"P1," + json.dumps(touch).encode() + b"\n")
                    event = await client.pending_touch()
                    self.assertEqual(event, touch)
                    if operation == 1:
                        self.assertFalse(hasattr(wire, "request"))
                    await session.process_touch(event)
                    self.assertEqual(wire.request["status"], status)
                    self.assertEqual(wire.request["operation"], operation)
                self.assertEqual([item.args[0] for item in launch.call_args_list],
                                 [["/usr/bin/open", f"codex://threads/{thread}"],
                                  ["/usr/bin/open", "-b", "com.google.antigravity"]])
                launch.side_effect = OSError("application unavailable")
                channel.events.put_nowait({**touch, "operation": 5, "id": records["antigravity"]})
                await session.process_touch(await client.pending_touch())
                self.assertEqual(wire.request["status"], "rejected")

            touch = {**touch, "operation": 6, "id": records["codex"]}
            channel.capture_event(b"P1," + json.dumps(touch).encode() + b"\n")
            self.assertEqual(await client.next_touch(), touch)
            self.assertEqual(wire.request["type"], "touch_ack")
            self.assertEqual(wire.request["status"], "rejected")

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

            async def show_text(self, _text, *, category=None):
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
            async def show_text(self, text, *, category=None):
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
