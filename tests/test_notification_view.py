import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import zlib

from deskpet_platform.notification_view import NotificationView
from deskpet_platform.notifications import NotificationStore
from deskpet_platform.mcp_server import BoardSession


class FakeChannel:
    def __init__(self):
        self.capabilities = {"agent_home": 1}
        self.boot = 101
        self.epoch = "e" * 32
        self.messages = []
        self.failure = None
        self.detail_receipt_crc = None

    async def send(self, message_type, **fields):
        packet = {"epoch": self.epoch, "seq": len(self.messages) + 1, "type": message_type, **fields}
        assert len(("P1," + json.dumps(packet, separators=(",", ":"))).encode()) <= 1024
        self.messages.append((message_type, fields))
        if self.failure == (message_type, fields.get("index")):
            raise OSError("synthetic disconnect")
        if message_type == "snapshot_commit":
            return {"committed_version": fields["version"]}
        if message_type == "detail_commit":
            return {"detail_ready": True}
        if message_type == "heartbeat":
            detail = next((fields for kind, fields in reversed(self.messages) if kind == "detail_begin"), {})
            crc = self.detail_receipt_crc if self.detail_receipt_crc is not None else detail.get("crc", 0)
            return {"detail_displayed": True, "detail_page": 0, "detail_revision": 1,
                    "detail_crc": crc}
        return {}


class FakeClient:
    def __init__(self):
        self.channel = FakeChannel()
        self._operation = asyncio.Lock()


class NotificationViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = NotificationStore(Path(self.temp.name) / "notifications.sqlite3")
        self.client = FakeClient()
        self.view = NotificationView(self.store)

    async def test_home_summarizes_many_sources_in_four_rows_without_repeated_sends(self):
        sources = ("codex", "claude_code", "workbuddy", "antigravity", "opencode")
        for index in range(50):
            self.store.add_agent_event(sources[index % 5], "s", str(index), "completed", "task")
        for source in sources:
            self.store.add_agent_event(source, "s", "ask", "response", "please respond")
        await self.view.refresh(self.client)
        messages = self.client.channel.messages
        begin = messages[0][1]
        self.assertTrue(begin["agent_home"])
        self.assertEqual((begin["count"], begin["results"], begin["waiting"]), (4, 50, 5))
        self.assertEqual(sum(kind == "row" for kind, _ in messages), 4)
        self.assertEqual(messages[-1][0], "snapshot_commit")
        await self.view.refresh(self.client)
        self.assertEqual(len(messages), 6)

    async def test_numeric_list_pages_keep_a_stable_anchor_and_reject_stale_touches(self):
        for index in range(113):
            self.store.add_agent_event("opencode", "s", str(index), "completed", "task")
        await self.view.refresh(self.client)
        self.assertFalse(self.view.touch({"action": "list", "version": 0, "id": ""}))
        self.assertTrue(self.view.touch({"action": "list", "version": 1, "id": ""}))
        await self.view.refresh(self.client)
        first = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"][-1]
        self.assertEqual(first["pagination"], [0, 29])
        original = self.view.rows[-1]["id"]
        self.store.add_agent_event("codex", "s", "later", "completed", "new")
        self.assertTrue(self.view.touch({"action": "next", "version": 2, "id": ""}))
        await self.view.refresh(self.client)
        second = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"][-1]
        self.assertEqual(second["pagination"], [1, 29])
        self.assertEqual(int(original[2:]) - 1, int(self.view.rows[0]["id"][2:]))

    async def test_details_are_checked_before_record_is_marked_seen(self):
        identity = self.store.add_agent_event("claude_code", "s", "ask", "response", "Project")
        await self.view.refresh(self.client)
        self.view.touch({"action": "list", "version": 1, "id": ""})
        await self.view.refresh(self.client)
        self.assertTrue(self.view.touch({"action": "detail", "version": 2, "id": f"n-{identity}"}))
        self.client.channel.failure = ("detail_chunk", 4)
        with self.assertRaises(OSError):
            await self.view.refresh(self.client)
        self.assertFalse(self.store.list()[0]["seen"])
        self.client.channel.failure = None
        self.client.channel.detail_receipt_crc = -1
        await self.view.refresh(self.client)
        chunks = [fields for kind, fields in self.client.channel.messages if kind == "detail_chunk"][-16:]
        begin = [fields for kind, fields in self.client.channel.messages if kind == "detail_begin"][-1]
        pixels = b"".join(bytes.fromhex(row["data"]) for row in chunks)
        self.assertEqual((len(pixels), zlib.crc32(pixels)), (4032, begin["crc"]))
        self.assertFalse(self.store.list()[0]["seen"])
        self.client.channel.detail_receipt_crc = None
        await self.view.refresh(self.client)
        self.assertTrue(self.store.list()[0]["seen"])
        self.assertFalse(self.store.list()[0]["resolved"])

    async def test_home_and_list_taps_survive_a_new_projection(self):
        identity = self.store.add_agent_event("codex", "s", "first", "completed", "First")
        await self.view.refresh(self.client)
        home_version = self.view.version
        self.store.add_agent_event("codex", "s", "second", "completed", "Second")
        await self.view.refresh(self.client)

        tap = {"action": "list", "version": home_version, "id": "", "epoch": self.view.epoch}
        self.assertFalse(self.view.touch({**tap, "version": self.view.version + 1}))
        self.assertFalse(self.view.touch({**tap, "epoch": "different connection"}))
        self.assertTrue(self.view.touch(tap))
        await self.view.refresh(self.client)
        list_version = self.view.version
        self.assertEqual(len(self.view.rows), 2)
        self.assertFalse(self.view.touch({**tap, "action": "detail", "id": f"n-{identity}"}))

        self.store.add_agent_event("codex", "s", "third", "completed", "Third")
        await self.view.refresh(self.client)
        self.assertGreater(self.view.version, list_version)
        self.assertTrue(self.view.touch({**tap, "action": "home", "version": list_version}))
        await self.view.refresh(self.client)
        self.assertEqual(self.view.mode, "home")
        self.assertEqual(sum(item["seen"] for item in self.store.list()), 0)

    async def test_reconnect_republishes_and_starts_new_version(self):
        await self.view.refresh(self.client)
        self.client.channel.epoch = "f" * 32
        await self.view.refresh(self.client)
        begins = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"]
        self.assertEqual([begin["version"] for begin in begins], [1, 1])

    async def test_reconnect_restores_selected_detail_on_a_later_page(self):
        for index in range(9):
            self.store.add_agent_event("codex", "s", str(index), "completed", f"Task {index}")
        await self.view.refresh(self.client)
        self.view.touch({"action": "list", "version": 1, "id": ""})
        await self.view.refresh(self.client, device_page=1)
        self.view.touch({"action": "next", "version": 2, "id": ""})
        await self.view.refresh(self.client, device_page=1)
        identity = self.view.rows[1]["id"]
        self.view.touch({"action": "detail", "version": 3, "id": identity})
        self.client.channel.failure = ("detail_chunk", 4)
        with self.assertRaises(OSError):
            await self.view.refresh(self.client, device_page=2)
        self.store.add_agent_event("codex", "s", "new", "completed", "New")
        self.client = FakeClient()
        self.client.channel.epoch = "g" * 32
        await self.view.refresh(self.client, device_page=2)
        begins = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"]
        self.assertEqual(begins[0]["pagination"], [1, 3])
        details = [fields for kind, fields in self.client.channel.messages if kind == "detail_begin"]
        self.assertEqual([fields["id"] for fields in details], [identity])
        self.assertEqual([item["id"] for item in self.store.list() if item["seen"]], [int(identity[2:])])

    async def test_reconnect_does_not_restore_detail_after_reboot_or_return_to_list(self):
        for reboot, page in ((True, 2), (False, 1)):
            with self.subTest(reboot=reboot, page=page):
                self.view = NotificationView(self.store)
                self.client = FakeClient()
                identity = self.store.add_agent_event("codex", "s", "done", "completed", "Task")
                await self.view.refresh(self.client, device_page=1)
                self.view.touch({"action": "detail", "version": 1, "id": f"n-{identity}"})
                self.client = FakeClient()
                self.client.channel.epoch = "g" * 32
                if reboot:
                    self.client.channel.boot += 1
                await self.view.refresh(self.client, device_page=page)
                self.assertFalse(any(kind.startswith("detail_") for kind, _ in self.client.channel.messages))
                self.assertFalse(self.store.list()[0]["seen"])

    async def test_response_attention_is_acknowledged_only_after_home_commit(self):
        self.client.channel.capabilities["response_attention"] = 1
        first = self.store.add_agent_event("codex", "s", "first", "response", "Project")
        await self.view.refresh(self.client, device_page=255)
        self.assertNotIn("response_attention", self.client.channel.messages[0][1])
        await self.view.refresh(self.client, device_page=0, role_ready=False)
        self.assertEqual(self.store.pending_attention(), first)
        self.assertEqual(len(self.client.channel.messages), 3)
        self.client.channel.failure = ("snapshot_commit", None)
        with self.assertRaises(OSError):
            await self.view.refresh(self.client, device_page=0)
        self.assertEqual(self.store.pending_attention(), first)
        self.client.channel.failure = None
        await self.view.refresh(self.client, device_page=0)
        self.assertEqual(self.store.pending_attention(), 0)
        self.client.channel.epoch = "g" * 32
        await NotificationView(self.store).refresh(self.client, device_page=0)
        begins = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"]
        self.assertEqual([begin.get("response_attention") for begin in begins], [None, f"{first:016x}", f"{first:016x}", None])

        second = self.store.add_agent_event("opencode", "another", "second", "response", "Project")
        self.store.mark_seen(second)
        self.assertTrue(self.store.has_active_notifications())
        self.assertEqual(self.store.pending_attention(), second)
        self.store.resolve_response(second)
        self.store.resolve_response(first)
        self.assertFalse(self.store.has_active_notifications())
        self.assertEqual(self.store.pending_attention(), 0)

    async def test_board_session_delivers_home_then_projects_the_touched_list(self):
        session = BoardSession(Path(self.temp.name) / "device.json")
        session.client = self.client
        session.notifications.add_agent_event("codex", "s", "q", "response", "Project")
        await session.refresh_agent_notifications({"pet_page": 0})
        begin = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"][-1]
        self.assertTrue(begin["agent_home"])
        await session.process_touch({"action": "list", "version": begin["version"], "id": ""})
        await session.refresh_agent_notifications({"pet_page": 1})
        listing = [fields for kind, fields in self.client.channel.messages if kind == "snapshot_begin"][-1]
        self.assertEqual(listing["pagination"], [0, 1])
        self.assertEqual(listing["count"], 1)


if __name__ == "__main__":
    unittest.main()
