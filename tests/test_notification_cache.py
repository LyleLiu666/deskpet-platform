import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from deskpet_platform.channel import CommandRejected
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.notification_cache import NotificationCache
from deskpet_platform.notifications import NotificationStore


class CacheBoard:
    boot = 12
    epoch = "e" * 32

    def __init__(self):
        self.capabilities = {"agent_home": 1, "agent_cache_selective": 1}
        self.events = asyncio.Queue()
        self.commands = []
        self.generation = 4
        self.receipts = []
        self.cache_available = True
        self.failure = None

    async def send(self, command, **fields):
        line = {"epoch": self.epoch, "seq": len(self.commands) + 1, "type": command, **fields}
        assert len(("P1," + json.dumps(line, separators=(",", ":")) + "\n").encode()) <= 1024
        self.commands.append((command, fields))
        if command == self.failure:
            raise CommandRejected("card_busy")
        if command == "cache_state":
            return {"cache_available": self.cache_available, "cache_generation": self.generation}
        if command == "cache_receipt_next":
            return self.receipts.pop(0) if self.receipts else {"has_receipt": False}
        if command == "cache_detail_begin":
            return {"next_chunk": 15}
        if command == "cache_commit":
            self.generation += 1
            return {"cache_generation": self.generation}
        if command == "snapshot_commit":
            return {"committed_version": fields["version"]}
        return {}


class TestNotificationCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = NotificationStore(Path(directory.name) / "notifications.sqlite3")
        self.cache = NotificationCache(self.store)
        self.channel = CacheBoard()
        self.client = type("Client", (), {"channel": self.channel, "_operation": asyncio.Lock()})()

    async def sync(self):
        for _ in range(25):
            await self.cache.step(self.client)
            if self.cache.transfer is None:
                return
        self.fail("cache transfer did not finish")

    async def test_verified_details_precede_list_and_offline_read_targets_only_matching_event(self):
        first = self.store.add_agent_event("codex", "session-a", "turn", "completed", "First")
        second = self.store.add_agent_event("claude_code", "session-b", "turn", "response", "第二" * 32)
        records = {record["id"]: record for record in self.store.cache_records()}
        wrong = records[second]["cache_id"].rsplit("-", 1)[0] + "-0000000000000000"
        self.channel.receipts = [
            {"has_receipt": True, "id": wrong, "content_revision": 1},
            {"has_receipt": True, "id": records[first]["cache_id"], "content_revision": 1},
        ]
        await self.sync()
        self.assertEqual([record["id"] for record in self.store.list() if record["seen"]], [first])
        operations = [command for command, _ in self.channel.commands]
        self.assertLess(operations.index("cache_receipt_finish"), operations.index("cache_detail_begin"))
        self.assertLess(max(i for i, cmd in enumerate(operations) if cmd == "cache_detail_commit"),
                        operations.index("cache_begin"))
        rows = [fields for command, fields in self.channel.commands if command == "cache_item"]
        self.assertEqual([(row["id"], row["unread"]) for row in rows],
                         [(records[second]["cache_id"], True), (records[first]["cache_id"], False)])
        chunks = [fields for command, fields in self.channel.commands if command == "cache_detail_chunk"]
        self.assertTrue(all(block["index"] == 15 and len(block["data"]) == 504 for block in chunks))

    async def test_old_firmware_never_gets_selective_cache(self):
        self.channel.capabilities = {"message_cache": 1, "agent_home": 1}
        await self.cache.step(self.client)
        self.assertEqual(self.channel.commands, [])

    async def test_first_reminder_waits_for_committed_card_and_shown_receipt_causes_no_upload(self):
        session = BoardSession(self.store.path.with_name("device.json"))
        session.client = self.client
        self.channel.capabilities["agent_cache_first"] = 1
        first = self.store.add_agent_event("codex", "s", "first", "completed", "First")
        self.channel.cache_available = False
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual([cmd for cmd, _ in self.channel.commands], ["cache_state"])
        self.assertFalse(self.store.list()[0]["shown"])

        self.channel.cache_available = True
        session.notification_cache.poll_at = 0
        await session.refresh_agent_notifications({"pet_page": 0})
        operations = [cmd for cmd, _ in self.channel.commands]
        self.assertLess(operations.index("cache_detail_commit"), operations.index("cache_commit"))
        self.assertLess(operations.index("cache_commit"), operations.index("snapshot_begin"))
        self.assertEqual([(item["id"], item["shown"]) for item in self.store.list()], [(first, True)])

        session.notification_cache.poll_at = 0
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual(sum(cmd == "cache_commit" for cmd, _ in self.channel.commands), 1)

        self.channel.epoch = "f" * 32
        self.assertIsNone(session.notification_cache.committed_records(self.channel))
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual(sum(cmd == "cache_commit" for cmd, _ in self.channel.commands), 1)
        self.assertEqual([item["id"] for item in session.notification_cache.committed_records(self.channel)], [first])

    async def test_later_reminder_survives_interrupted_card_write_and_does_not_publish_early(self):
        session = BoardSession(self.store.path.with_name("device.json"))
        session.client = self.client
        self.channel.capabilities["agent_cache_first"] = 1
        first = self.store.add_agent_event("codex", "s", "first", "completed", "First")
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual([item["id"] for item in session.notification_cache.committed_records(self.channel)], [first])
        self.channel.failure = "cache_commit"
        newer = self.store.add_agent_event("workbuddy", "s", "next", "completed", "New")
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual([item["id"] for item in session.notification_cache.committed_records(self.channel)], [first])
        self.assertFalse(self.store.list()[0]["shown"])

        self.channel.failure = None
        self.channel.epoch = "f" * 32
        self.assertIsNone(session.notification_cache.committed_records(self.channel))
        await session.refresh_agent_notifications({"pet_page": 0})
        self.assertEqual([item["id"] for item in session.notification_cache.committed_records(self.channel)], [newer, first])
        self.assertTrue(self.store.list()[0]["shown"])

    async def test_previous_agent_firmware_publishes_home_without_waiting_for_the_card(self):
        session = BoardSession(self.store.path.with_name("device.json"))
        session.client = self.client
        self.channel.cache_available = False
        self.store.add_agent_event("codex", "s", "first", "completed", "First")
        await session.refresh_agent_notifications({"pet_page": 0})
        operations = [cmd for cmd, _ in self.channel.commands]
        self.assertLess(operations.index("snapshot_commit"), operations.index("cache_state"))
        self.assertTrue(self.store.list()[0]["shown"])
