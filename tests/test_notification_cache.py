import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.channel import CommandRejected
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.notification_cache import NotificationCache, _fingerprint, _prepare
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
        self.rows = []
        self.pending = []
        self.declared_count = 0
        self.pending_item = None
        self.pending_bitmap = None
        self.cached_details = False
        self.copy_failure = False
        self.manifest_changed = False

    async def send(self, command, **fields):
        line = {"epoch": self.epoch, "seq": len(self.commands) + 1, "type": command, **fields}
        assert len(("P1," + json.dumps(line, separators=(",", ":")) + "\n").encode()) <= 1024
        self.commands.append((command, fields))
        if command == self.failure:
            raise CommandRejected("card_busy")
        if command == "cache_state":
            return {"cache_available": self.cache_available, "cache_generation": self.generation,
                    "cache_count": len(self.rows)}
        if command == "cache_receipt_next":
            return self.receipts.pop(0) if self.receipts else {"has_receipt": False}
        if command == "cache_detail_begin":
            return {"cached": self.cached_details, "next_chunk": 15}
        if command in ("cache_manifest", "cache_copy"):
            start, size = fields["start"], fields["count"]
            assert fields["generation"] == self.generation and 0 < size <= 8 and start + size <= len(self.rows)
            selected = self.rows[start:start + size]
            if command == "cache_manifest":
                return {"cache_changed": True} if self.manifest_changed else {"rows": selected}
            if self.copy_failure:
                self.copy_failure = False
                self.pending = []
                return {"cache_reuse_failed": True}
            self.pending.extend(selected)
        if command == "cache_begin":
            self.pending = []
            self.declared_count = fields["count"]
        if command == "cache_abort":
            self.pending = []
        if command == "cache_item":
            self.pending_item = fields
        if command == "cache_bitmap":
            self.pending_bitmap = fields["bitmap"]
        if command == "cache_item_commit":
            self.pending.append(_fingerprint(self.pending_item, self.pending_bitmap))
        if command == "cache_commit":
            assert len(self.pending) == self.declared_count
            self.rows = list(self.pending)
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

    async def test_explicit_dismissal_receipt_removes_only_that_reminder(self):
        self.channel.capabilities.update(agent_inbox_read=1, agent_local_inbox=1, cache_feedback_dismissal=1)
        question = self.store.add_agent_event("codex", "session", "q", "response", "Question")
        record = self.store.cache_records(active_only=True)[0]
        self.channel.receipts = [{"has_receipt": True, "id": record["cache_id"],
                                  "content_revision": 1, "feedback_dismissal": True}]
        await self.sync()
        self.assertEqual(self.store.cache_records(active_only=True), [])
        self.assertFalse(self.store.list()[0]["resolved"])
        self.assertEqual(self.store.list()[0]["id"], question)
        self.assertIn(("cache_receipt_begin", {"feedback_dismissal": True}), self.channel.commands)

    async def test_feedback_card_uses_local_confirmation_without_detail_transfer(self):
        self.channel.capabilities.update(agent_inbox_read=1, agent_local_inbox=1, cache_feedback_dismissal=1)
        self.store.add_agent_event("claude_code", "1234567890", "question", "response", "repository")
        await self.sync()
        item = next(fields for command, fields in self.channel.commands if command == "cache_item")
        self.assertEqual(item["kind"], 5)
        self.assertEqual(item["detail_crc"], [])
        self.assertNotIn("cache_detail_begin", [command for command, _ in self.channel.commands])

    async def test_invalid_dismissal_is_never_acknowledged(self):
        self.channel.capabilities.update(agent_inbox_read=1, agent_local_inbox=1, cache_feedback_dismissal=1)
        self.store.add_agent_event("codex", "session", "q", "response", "Project")
        self.channel.receipts = [{"has_receipt": True, "id": "p-1-forged", "content_revision": 1,
                                  "feedback_dismissal": True}]
        await self.sync()
        self.assertEqual(len(self.store.cache_records(active_only=True)), 1)
        operations = [command for command, _ in self.channel.commands]
        self.assertIn("cache_abort", operations)
        self.assertNotIn("cache_receipt_ack", operations)

    async def test_local_inbox_keeps_one_question_per_session_and_drops_read_completions(self):
        self.channel.capabilities.update(agent_inbox_read=1, agent_local_inbox=1)
        old = self.store.add_agent_event("codex", "same-session", "old", "response", "Old", parent_turn="turn-1")
        question = self.store.add_agent_event("codex", "same-session", "new", "response", "New", parent_turn="turn-2")
        other = self.store.add_agent_event("codex", "other-session", "other", "response", "Other")
        complete = self.store.add_agent_event("codex", "same-session", "done", "completed", "Done")
        self.store.mark_seen(complete)
        await self.sync()
        card = self.cache.committed_records(self.channel)
        self.assertEqual([item["id"] for item in card], [other, question])
        self.store.mark_seen(question)
        self.store.resolve_agent_event("codex", "same-session", "new")
        self.cache.poll_at = 0
        await self.sync()
        self.assertEqual([item["id"] for item in self.cache.committed_records(self.channel)], [other])
        self.assertFalse(next(item for item in self.store.list() if item["id"] == old)["resolved"])

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

    async def test_local_completed_detail_retains_formal_grayscale_across_resume(self):
        self.channel.capabilities["agent_local_inbox"] = 1
        self.store.add_agent_event("codex", "session", "done", "completed", "修复任务状态显示")
        record, = self.store.cache_records(active_only=True)
        _, _, _, pixels = _prepare(record, finished_gray=True)
        await self.sync()
        detail, = [fields for command, fields in self.channel.commands if command == "cache_detail_begin"]
        blocks = [fields for command, fields in self.channel.commands if command == "cache_detail_chunk"]
        self.assertTrue(detail["gray"])
        self.assertEqual(len(pixels), 8064)
        self.assertEqual([block["index"] for block in blocks], list(range(15, 32, 2)))
        self.assertEqual(len(blocks[-1]["data"]), 504)

    async def test_poll_shortens_the_wait_between_card_batches_only_while_transferring(self):
        session = BoardSession(self.store.path.with_name("device.json"))
        session.client = self.client
        self.channel.capabilities["agent_cache_first"] = 1
        self.channel.dropped_events = 0

        async def status():
            return {"pet_page": 0}

        self.client.status = status
        for index in range(8):
            self.store.add_agent_event("codex", "s", str(index), "completed", f"Message {index}")
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 1:
                self.assertFalse(any(row["shown"] for row in self.store.list()))
            if len(delays) == 4:
                raise asyncio.CancelledError

        with patch("deskpet_platform.mcp_server.asyncio.sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await session.poll()
        self.assertLess(delays[0], 1)
        self.assertEqual(delays[-1], 1)
        operations = [cmd for cmd, _ in self.channel.commands]
        self.assertLess(operations.index("cache_commit"), operations.index("snapshot_begin"))
        self.assertTrue(all(row["shown"] for row in self.store.list()))

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

    async def test_cold_reconnect_reuses_committed_card_only_when_manifest_matches(self):
        self.channel.capabilities.update(agent_cache_first=1, agent_cache_reuse=1)
        for index in range(17):
            self.store.add_agent_event("codex", "s", str(index), "completed", f"First {index}")
        await self.sync()
        self.channel.commands.clear()
        self.channel.epoch = "f" * 32
        self.cache = NotificationCache(self.store)
        generation = self.channel.generation
        self.assertIsNone(self.cache.committed_records(self.channel))
        await self.sync()
        self.assertEqual(self.channel.generation, generation)
        self.assertEqual(len(self.cache.committed_records(self.channel)), 17)
        self.assertEqual([fields["count"] for cmd, fields in self.channel.commands if cmd == "cache_manifest"], [8, 8, 1])
        self.assertFalse(any(cmd in ("cache_begin", "cache_commit", "cache_detail_begin") for cmd, _ in self.channel.commands))

    async def test_one_new_row_reuses_68_previously_committed_rows(self):
        self.channel.capabilities.update(agent_cache_first=1, agent_cache_reuse=1)
        for index in range(68):
            self.store.add_agent_event("codex", "s", str(index), "completed", f"Original {index}")

        def fingerprint(record):
            item, bitmap, _, _ = _prepare(record)
            return _fingerprint(item, bitmap)

        self.channel.rows = old = [fingerprint(record) for record in self.store.cache_records()]
        newer = self.store.add_agent_event("codex", "s", "next", "completed", "New")
        await self.sync()
        self.assertEqual(self.channel.rows, [fingerprint(self.store.cache_records()[0]), *old])
        self.assertEqual(self.cache.committed_records(self.channel)[0]["id"], newer)
        serialized = [item for command, item in self.channel.commands if command == "cache_item"]
        self.assertEqual(len(serialized), 1)
        self.assertEqual(sum(cmd == "cache_detail_begin" for cmd, _ in self.channel.commands), 1)
        self.assertEqual(sum(item["count"] for cmd, item in self.channel.commands if cmd == "cache_copy"), 68)
        self.assertLess(len(self.channel.commands), 50)

    async def test_generation_change_and_copy_failure_retain_old_commit_until_full_retry(self):
        self.channel.capabilities.update(agent_cache_first=1, agent_cache_reuse=1)
        first = self.store.add_agent_event("codex", "s", "first", "completed", "First")
        await self.sync()
        self.channel.commands.clear()
        self.channel.copy_failure = self.channel.manifest_changed = True
        newer = self.store.add_agent_event("codex", "s", "next", "completed", "New")
        original = self.channel.rows
        await self.sync()
        self.assertEqual(self.channel.rows, original)
        self.assertFalse(any(cmd in ("cache_begin", "cache_commit") for cmd, _ in self.channel.commands))

        self.channel.manifest_changed = False
        self.cache.poll_at = 0
        await self.sync()
        self.assertEqual(self.channel.rows, original)
        self.assertEqual([item["id"] for item in self.cache.committed_records(self.channel)], [first])
        self.assertTrue(self.cache.reuse_failed)
        self.cache.poll_at = 0
        self.channel.commands.clear()
        await self.sync()
        self.assertEqual([item["id"] for item in self.cache.committed_records(self.channel)], [newer, first])
        self.assertFalse(any(cmd in ("cache_manifest", "cache_copy") for cmd, _ in self.channel.commands))
        self.assertFalse(self.cache.reuse_failed)
