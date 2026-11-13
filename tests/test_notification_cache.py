import asyncio
import json
from pathlib import Path
import tempfile
import unittest

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

    async def send(self, command, **fields):
        line = {"epoch": self.epoch, "seq": len(self.commands) + 1, "type": command, **fields}
        assert len(("P1," + json.dumps(line, separators=(",", ":")) + "\n").encode()) <= 1024
        self.commands.append((command, fields))
        if command == "cache_state":
            return {"cache_available": True, "cache_generation": self.generation}
        if command == "cache_receipt_next":
            return self.receipts.pop(0) if self.receipts else {"has_receipt": False}
        if command == "cache_detail_begin":
            return {"next_chunk": 15}
        if command == "cache_commit":
            self.generation += 1
            return {"cache_generation": self.generation}
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
