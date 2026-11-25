import sqlite3
from contextlib import closing
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import fcntl
import os
import asyncio
import time

from deskpet_platform import notifications
from deskpet_platform.channel import CommandRejected
from deskpet_platform.codex_hook import install_hook, remove_hook, receive, receive_event
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig


class NotificationTests(unittest.TestCase):
    def test_old_display_receipt_keeps_withdrawal_pending_across_desktop_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            older = notifications.NotificationStore(path)
            displayed = older.add_agent_event("codex", "session", "q", "response", "Question")
            older.mark_home_shown([displayed])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DROP TABLE notification_response_projections")

            upgraded = notifications.NotificationStore(path)
            self.assertTrue(upgraded.resolve_response(displayed))
            self.assertTrue(upgraded.has_active_notifications(include_unconfirmed=True))
            upgraded.record_response_projection("screen", [], confirmed=True)
            self.assertFalse(upgraded.has_active_notifications(include_unconfirmed=True))

    def test_new_receiver_inherits_history_without_replaying_it(self):
        from deskpet_platform.device_registry import DeviceRegistry
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(config)
            source = notifications.NotificationStore(config.parent / 'notifications.sqlite3')
            old = source.add_agent_event('codex', 's', 'old', 'completed', 'old')
            source.mark_seen(old)
            source.add_agent_event('codex', 's', 'unread', 'completed', 'unread')
            source.add_agent_event('codex', 's', 'question', 'response', 'question')
            receiver = DeviceRegistry(config).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            inbox = notifications.NotificationStore(receiver.parent / 'notifications.sqlite3', source=source)
            self.assertEqual(inbox.unseen_count(), 2)
            self.assertIsNone(inbox.next_pending())
            self.assertEqual(inbox.pending_attention(), 0)
            self.assertEqual(inbox.home_summary()['response_count'], 1)
            source.add_agent_event('codex', 's', 'new', 'completed', 'new')
            inbox.sync()
            self.assertEqual(inbox.next_pending()['title'], 'new')
            inbox.mark_seen(inbox.next_pending()['id'])
            self.assertFalse(source.list()[0]['seen'])

    @unittest.skipIf(os.name == 'nt', 'POSIX permission test')
    def test_receiver_registration_rolls_back_if_history_cannot_be_read(self):
        from deskpet_platform.device_registry import DeviceRegistry
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(config)
            source = notifications.NotificationStore(config.parent / 'notifications.sqlite3')
            source.add_agent_event('codex', 's', 'old', 'completed', 'old')
            source.path.chmod(0o644)
            registry = DeviceRegistry(config)
            with self.assertRaises(ValueError):
                registry.add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            self.assertEqual(registry.receiver_paths(), [])

    def test_new_event_starts_independent_receiver_worker_when_registered(self):
        from deskpet_platform.device_registry import DeviceRegistry
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'device.json'
            DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(config)
            registry=DeviceRegistry(config)
            registry.add_receiver(DeviceConfig('02:00:00:00:00:02',bytes(range(32))))
            event={'hook_event_name':'Stop','session_id':'s','turn_id':'t'}
            with patch('deskpet_platform.codex_hook.subprocess.Popen') as spawn:
                receive_event(config,event)
            self.assertEqual(spawn.call_count,2)
            self.assertEqual([call.args[0][-1] for call in spawn.call_args_list],['notification-worker','receiver-worker'])

    def test_dismissal_and_answer_clear_the_shared_reminder_without_syncing_read_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = notifications.NotificationStore(root / "source.sqlite3")
            first = notifications.NotificationStore(root / "first.sqlite3", source=source)
            second = notifications.NotificationStore(root / "second.sqlite3", source=source)
            # Receiver row numbers need not match the shared or other board's numbers.
            first.add_agent_event("codex", "other", "local", "completed", "Local")
            first.mark_seen(1)
            question = source.add_agent_event("codex", "session", "question", "response", "Project")
            other = source.add_agent_event("codex", "other", "question", "response", "Another project")
            first.sync()
            second.sync()
            record = next(row for row in first.cache_records(active_only=True) if row["title"] == "Project")
            first.mark_cached_seen(record["cache_id"])
            second.sync()
            self.assertEqual(second.home_summary()["response_count"], 2)
            self.assertFalse(second.get(question)["seen"])
            self.assertFalse(first.dismiss_cached_response(record["cache_id"] + "bad"))
            self.assertTrue(first.dismiss_cached_response(record["cache_id"]))
            # Replayed receipt after reconnect is safe.
            self.assertTrue(first.dismiss_cached_response(record["cache_id"]))
            self.assertEqual(source.home_summary()["response_count"], 1)
            second.sync()
            for inbox in (first, second, source):
                self.assertEqual([item["title"] for item in inbox.cache_records(active_only=True)], ["Another project"])
                self.assertNotIn("Project", [item["title"] for item in inbox.cache_records()])
            self.assertFalse(second.get(question)["resolved"])
            self.assertFalse(second.get(question)["seen"])
            self.assertEqual(second.unseen_count(), 2)
            self.assertEqual(second.unseen_count(for_device=True), 1)
            self.assertNotIn(question, [item["id"] for item in second.list_page(1)["items"]])
            self.assertEqual(second.next_pending()["id"], other)

            late = notifications.NotificationStore(root / "late.sqlite3", source=source)
            late.sync()
            self.assertEqual(late.home_summary()["response_count"], 1)
            source.resolve_agent_event("codex", "other", "question")
            for inbox in (first, second, late):
                inbox.sync()
                self.assertEqual(inbox.home_summary()["response_count"], 0)
                self.assertFalse(inbox.has_active_notifications())
            source.resolve_agent_event("codex", "session", "question")
            second.sync()
            self.assertTrue(second.get(question)["resolved"])
            self.assertFalse(second.get(question)["seen"])

    def test_multiple_sources_have_explicit_categories_and_bounded_home_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            sources = ("codex", "claude_code", "workbuddy", "antigravity", "opencode")
            for index in range(50):
                store.add_agent_event(sources[index % len(sources)], "session", str(index), "completed", "task")
            for index, source in enumerate(sources):
                store.add_agent_event(source, "session", "ask", "response", f"request {index}")
            # A duplicate from the same source and event does not count twice.
            store.add_agent_event("codex", "session", "ask", "response", "request 0")
            summary = store.home_summary()
            self.assertEqual(summary["completed_count"], 5)
            self.assertEqual(set(summary["completed_sources"]), set(sources))
            self.assertEqual(len(summary["response_sources"]), 5)
            self.assertEqual(summary["response_source_count"], 5)
            self.assertTrue(all(row["count"] == 1 for row in summary["response_sources"]))
            items = store.list()
            self.assertEqual((items[0]["source"], items[0]["category"]), ("opencode", "response"))
            store.mark_seen(items[0]["id"])
            # Looking at a request is not the same as answering it.
            self.assertEqual(store.home_summary()["response_source_count"], 5)
            self.assertTrue(store.resolve_response(items[0]["id"]))
            self.assertEqual(store.home_summary()["response_source_count"], 4)
            completed = next(item for item in store.list() if item["category"] == "completed")
            self.assertFalse(store.resolve_response(completed["id"]))
            self.assertTrue(store.list()[0]["seen"])

    def test_active_cache_keeps_newest_completion_per_source_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            turns = [store.add_agent_event("codex", "session", str(turn), "completed", str(turn))
                     for turn in range(5)]
            other = store.add_agent_event("claude_code", "session", "done", "completed", "Other agent")
            question = store.add_agent_event("codex", "session", "q", "response", "Question")
            card = store.cache_records(active_only=True, pending_first=True)
            self.assertEqual([item["id"] for item in card], [question, other, turns[-1]])
            self.assertEqual(len(store.list()), 7)

    def test_notification_pages_are_unbounded_and_snapshot_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            for index in range(113):
                store.add_agent_event("opencode", "s", str(index), "completed", f"task {index}")
            first = store.list_page(1, page_size=10)
            self.assertEqual((first["total"], first["pages"], first["page"]), (113, 12, 1))
            store.add_agent_event("codex", "s", "new", "completed", "new")
            second = store.list_page(2, page_size=10, anchor=first["anchor"])
            last = store.list_page(12, page_size=10, anchor=first["anchor"])
            self.assertEqual(second["total"], 113)
            self.assertEqual(len(last["items"]), 3)
            self.assertEqual(first["items"][-1]["id"] - 1, second["items"][0]["id"])
            self.assertEqual(store.list_page(1, page_size=10)["total"], 114)

    def test_agent_event_rejects_missing_source_or_ambiguous_category(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            for source, category in (("", "completed"), ("codex", "unknown"), ("bad source", "response")):
                with self.assertRaises(ValueError):
                    store.add_agent_event(source, "s", "e", category, "task")
            self.assertEqual(store.list(), [])

    def test_agent_event_conflicting_duplicate_does_not_change_existing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            original = store.add_agent_event("claude_code", "session", "request", "response", "Approval")
            with self.assertRaises(ValueError):
                store.add_agent_event("claude_code", "session", "request", "completed", "Approval")
            item = store.list()[0]
            self.assertEqual((item["id"], item["category"]), (original, "response"))

    def test_codex_event_is_deduplicated_and_never_changes_codex_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "state_5.sqlite"
            with closing(sqlite3.connect(codex)) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
                db.execute("CREATE TABLE thread_read_receipts (id TEXT, unread INTEGER)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("session-1", "我的任务", "标题"))
                db.execute("INSERT INTO thread_read_receipts VALUES (?, ?)", ("session-1", 1))
            store = notifications.NotificationStore(root / "desk" / "notifications.sqlite3")
            event = {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1",
                     "cwd": "/tmp/example", "last_assistant_message": "private reply"}
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                first = store.add_codex_event(event)
                self.assertEqual(store.add_codex_event(event), first)
            items = store.list()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "我的任务")
            self.assertNotIn("private reply", str(items))
            self.assertEqual(store.next_pending()["id"], first)
            self.assertEqual(store.latest_unseen()["id"], first)
            store.mark_shown(first)
            self.assertIsNone(store.next_pending())
            store.mark_seen(first)
            self.assertEqual(store.unseen_count(), 0)
            self.assertIsNone(store.latest_unseen())
            with closing(sqlite3.connect(codex)) as db:
                self.assertEqual(db.execute("SELECT unread FROM thread_read_receipts").fetchone()[0], 1)
            self.assertEqual((root / "desk" / "notifications.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_codex_title_uses_the_selected_profile_without_reading_the_default(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "custom profile"
            profile.mkdir()
            with closing(sqlite3.connect(profile / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("custom", "Custom title", "fallback"))
            with patch.dict(os.environ, {"CODEX_HOME": str(profile)}), \
                 patch("deskpet_platform.notifications.sqlite3.connect", wraps=sqlite3.connect) as connect:
                self.assertEqual(notifications.session_title("custom", "/tmp/fallback"), "Custom title")
                self.assertEqual(notifications.session_title("missing", "/tmp/fallback"), "fallback")
            self.assertEqual([call.args[0] for call in connect.call_args_list],
                             [(profile / "state_5.sqlite").resolve().as_uri() + "?mode=ro"] * 2)

    def test_invalid_or_non_stop_events_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            for event in ({"hook_event_name": "Stop", "session_id": "", "turn_id": "t"},
                          {"hook_event_name": "Other", "session_id": "s", "turn_id": "t"}):
                with self.assertRaises(ValueError):
                    store.add_codex_event(event)
            self.assertEqual(store.list(), [])

    def test_hook_install_preserves_existing_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]},
                                      {"matcher": "*", "hooks": []}],
                             "Other": [{"hooks": [{"type": "command", "command": "untouched"}]}]}}
            path.write_text(json.dumps(old), encoding="utf-8")
            path.chmod(0o600)
            self.assertTrue(install_hook(path, "deskpet hook"))
            self.assertFalse(install_hook(path, "deskpet hook"))
            updated = json.loads(path.read_text())
            self.assertEqual(updated["hooks"]["Stop"][0], old["hooks"]["Stop"][0])
            self.assertEqual(updated["hooks"]["Other"], old["hooks"]["Other"])
            self.assertEqual(len(updated["hooks"]["Stop"]), 3)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(remove_hook(path, "deskpet hook"))
            self.assertFalse(remove_hook(path, "deskpet hook"))
            self.assertEqual(json.loads(path.read_text())["hooks"], old["hooks"])

            install_hook(path, "deskpet hook")
            updated = json.loads(path.read_text())
            updated["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "later"}]})
            path.write_text(json.dumps(updated))
            self.assertTrue(remove_hook(path, "deskpet hook"))
            after = json.loads(path.read_text())
            self.assertEqual(after["hooks"]["Stop"][2]["hooks"], [])
            self.assertEqual(after["hooks"]["Stop"][3]["hooks"][0]["command"], "later")
            after["hooks"]["Stop"].append({"hooks": [
                {"type": "command", "command": "deskpet hook"},
                {"type": "command", "command": "later-in-group"}]})
            path.write_text(json.dumps(after))
            with self.assertRaises(ValueError):
                remove_hook(path, "deskpet hook")

    def test_hook_records_private_local_event_and_starts_a_background_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "device.json"
            payload = {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t",
                       "cwd": "/tmp/demo", "transcript_path": None, "model": "fixture",
                       "permission_mode": "default", "stop_hook_active": False,
                       "last_assistant_message": "private reply"}
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output), \
                 patch.dict(os.environ, {"CODEX_HOME": str(config.parent)}), \
                 patch("deskpet_platform.codex_hook.subprocess.Popen") as spawn:
                receive(config)
            self.assertEqual(output.getvalue(), "{}\n")
            self.assertEqual(spawn.call_count, 1)
            store = notifications.NotificationStore(config.parent / "notifications.sqlite3")
            items = store.list()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "demo")
            self.assertNotIn(b"private reply", store.path.read_bytes())

    def test_foreground_can_request_background_to_release_board(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "device.json"
            self.assertFalse(notifications.request_worker_yield(config))
            lock = os.open(config.parent / "notification-worker.lock", os.O_RDWR)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(notifications.request_worker_yield(config))
                self.assertTrue((config.parent / "notification-handoff").exists())
            finally:
                os.close(lock)


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_inbox_keeps_backlog_unread_and_delivers_a_fresh_single(self):
        class Board:
            class Channel:
                capabilities = {}
            channel = Channel()
            def __init__(self):
                self.texts = []

            async def show_text(self, text, *, category=None):
                self.texts.append(text)
                return {"status": "shown"}

        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            with patch.object(notifications.time, "time", return_value=100):
                for i in range(15):
                    store.add_agent_event("codex", "s", str(i), "completed", "Old")
                request = store.add_agent_event("codex", "s", "old-request", "response", "Still open")
            with patch.object(notifications.time, "time", return_value=3700):
                fresh = store.add_agent_event("codex", "s", "fresh", "completed", "Fresh")
                store.summarize_legacy_backlog()
                board = Board()
                self.assertTrue(await notifications.deliver_next(board, store))
                self.assertFalse(await notifications.deliver_next(board, store))
                self.assertEqual(board.texts, ["Codex 完成\nFresh"])
                self.assertEqual(store.unseen_count(for_device=True), 17)
                self.assertFalse(store.get(request)["resolved"])
                self.assertTrue(store.get(fresh)["shown"])

    async def test_legacy_inbox_coalesces_only_after_a_confirmed_toast(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            completion = store.add_agent_event("codex", "s", "one", "completed", "One")
            remaining = store.add_agent_event("claude_code", "s", "two", "response", "Two")
            withdrawn = store.add_agent_event("codex", "s2", "three", "response", "Three")
            store.summarize_legacy_backlog()
            self.assertEqual(store.next_pending(legacy_batch=True)["id"], withdrawn)
            store.resolve_response(withdrawn)
            board = type("Board", (), {"channel": type("Channel", (), {"capabilities": {}})()})()
            board.show_text = AsyncMock(return_value={"status": "expired"})
            self.assertFalse(await notifications.deliver_next(board, store, legacy_batch=True))
            self.assertFalse(store.get(completion)["shown"])
            self.assertFalse(store.get(remaining)["shown"])
            board.show_text.return_value = {"status": "shown"}
            self.assertTrue(await notifications.deliver_next(board, store, legacy_batch=True))
            self.assertIn("Claude Code", board.show_text.call_args.args[0])
            self.assertEqual(board.show_text.call_args.kwargs, {"category": "response"})
            self.assertEqual(store.unseen_count(for_device=True), 2)
            self.assertIsNone(store.next_pending())

    async def test_a_notice_arriving_during_send_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_agent_event("codex", "s", "one", "completed", "One")
            class Board:
                channel = type("Channel", (), {"capabilities": {}})()
                async def show_text(self, text, *, category=None):
                    store.add_agent_event("codex", "s2", "two", "response", "Two")
                    return {"status": "shown"}
            self.assertTrue(await notifications.deliver_next(Board(), store, legacy_batch=True))
            self.assertEqual(store.next_pending()["title"], "Two")

    async def test_poll_summarizes_backlog_only_for_legacy_message_cache_boards(self):
        from unittest.mock import AsyncMock
        from deskpet_platform.mcp_server import BoardSession

        class Board:
            def __init__(self, capabilities):
                self.channel = type("Channel", (), {"capabilities": capabilities})()
                self.status = AsyncMock(return_value={"pet_page": 1})
                self.pending_notice_reads = AsyncMock(return_value=[])
                self.sync_messages = AsyncMock()
                self.set_notice_count = AsyncMock()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, caps, expected_summarized in (
                ("agent-home", {"notices": 1, "message_cache": 1, "agent_home": 1}, False),
                ("text-only", {"notices": 1, "text_present": 1}, False),
                ("legacy-cache", {"notices": 1, "message_cache": 1}, True),
            ):
                session = BoardSession(root / name / "device.json", no_token_probe=True)
                session.client = Board(caps)
                with patch.object(notifications.time, "time", return_value=100):
                    item = session.notifications.add_agent_event("codex", "s", "old", "completed", "Old")
                with patch.object(session, "refresh_agent_notifications", AsyncMock()), \
                     patch("deskpet_platform.mcp_server.asyncio.sleep", side_effect=asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await session.poll()
                self.assertEqual(session.notifications.get(item)["shown"], expected_summarized, name)

    async def test_hidden_screen_defers_delivery_without_marking_shown(self):
        from deskpet_platform.channel import TextNotVisible
        class Board:
            class Channel:
                capabilities = {}
            channel = Channel()
            async def show_text(self, _text, *, category=None):
                raise TextNotVisible('hidden')
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / 'notifications.sqlite3')
            store.add_agent_event('claude_code', 's', 'ask', 'response', 'Review')
            self.assertFalse(await notifications.deliver_next(Board(), store))
            self.assertFalse(store.list()[0]['shown'])
            self.assertIsNotNone(store.next_pending())

    async def test_response_names_its_source_and_does_not_play_finished_action(self):
        class Board:
            class Channel:
                capabilities = {"role_actions": 1}
            channel = Channel()
            text = None
            async def status(self):
                raise AssertionError("a response request is not a completed action")
            async def show_text(self, text, *, category=None):
                self.text = text
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            identity = store.add_agent_event("claude_code", "s", "ask-1", "response", "Review")
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertIn("Claude Code", board.text)
            self.assertIn("需要应答", board.text)
            self.assertNotIn("Codex", board.text)
            self.assertTrue(store.list()[0]["shown"])
            self.assertFalse(store.list()[0]["seen"])
            self.assertEqual(store.list()[0]["id"], identity)

    async def test_response_resolved_before_delivery_is_not_shown_as_pending(self):
        class Board:
            class Channel:
                capabilities = {}
            channel = Channel()
            async def show_text(self, _text, *, category=None):
                raise AssertionError("an answered request must not alert as pending")
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            identity = store.add_agent_event("opencode", "s", "request", "response", "Approve")
            self.assertTrue(store.resolve_response(identity))
            self.assertFalse(await notifications.deliver_next(Board(), store))
            self.assertFalse(store.list()[0]["seen"])

    async def test_pending_completion_action_does_not_mark_response_as_action_retry(self):
        class Board:
            class Channel:
                capabilities = {"role_actions": 1}
            channel = Channel()
            actions = 0
            async def status(self):
                return {"role_state": "ready", "finish_action": "finish"}
            async def list_actions(self):
                return ["finish"]
            async def play_role(self, *_args):
                self.actions += 1
                return {"action_status": "busy" if self.actions <= 2 else "started"}
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_agent_event("codex", "s", "done", "completed", "task")
            board = Board()
            self.assertFalse(await notifications.deliver_next(board, store))
            store.add_agent_event("claude_code", "s", "ask", "response", "request")
            self.assertFalse(await notifications.deliver_next(board, store))
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertIsNone(store.next_pending())
            self.assertEqual(board.actions, 3)

    async def test_offline_read_is_persisted_before_ack_and_does_not_mark_another_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = BoardSession(root / "device.json")
            with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
                first = session.notifications.add_codex_event(
                    {"hook_event_name": "Stop", "session_id": "one", "turn_id": "turn"})
                second = session.notifications.add_codex_event(
                    {"hook_event_name": "Stop", "session_id": "two", "turn_id": "turn"})

            class Device:
                channel = type("Channel", (), {"capabilities": {}})()
                pending = [first]
                commits = 0
                reject_first_ack = True

                async def pending_notice_reads(self):
                    return self.pending.copy()

                async def acknowledge_notice_reads(self, ids):
                    records = {item["id"]: item["seen"] for item in session.notifications.list()}
                    assert records == {first: True, second: False}
                    assert ids == [first]
                    if self.reject_first_ack:
                        self.reject_first_ack = False
                        raise OSError("synthetic disconnect")
                    self.pending.clear()

                async def sync_messages(self, records):
                    assert [(row["id"], row["seen"]) for row in records] == [(second, False), (first, True)]
                    self.commits += 1

            session.client = board = Device()
            with self.assertRaises(OSError):
                await session.refresh_messages()
            self.assertEqual(board.pending, [first])
            await session.refresh_messages()
            await session.refresh_messages()
            self.assertEqual(board.pending, [])
            self.assertEqual(board.commits, 1)

    async def test_card_summary_applies_clear_watermark_and_dismiss_before_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = notifications.NotificationStore(root / "shared.sqlite3")
            session = BoardSession(root / "device" / "device.json", notification_source=shared)
            for i in range(12):
                shared.add_agent_event("codex", f"session-{i}", "done", "completed", "old completion")
            shared.add_agent_event("codex", "session-0", "done-again", "completed", "same session")
            shared.add_agent_event("codex", "first-question", "ask", "response", "old request")
            shared.add_agent_event("codex", "second-question", "ask", "response", "live request")
            session.notifications.sync()
            rows, summary = session.notifications.legacy_message_snapshot(0, 0)
            self.assertEqual(summary, (12, 2))
            self.assertEqual(len(rows), 10)
            latest, older_question = rows[:2]
            clear = latest["id"], int(latest["created_at"])
            session.notifications.legacy_message_snapshot(clear[0], clear[1] + 1)
            self.assertEqual(session.notifications.home_summary()["completed_count"], 12)
            shared.add_agent_event("codex", "later-session", "done", "completed", "arrived after clear")
            session.notifications.sync()

            class Device:
                channel = type("Channel", (), {"capabilities": {"message_card_summary": 1}})()
                pending = [{"id": older_question["id"], "created": int(older_question["created_at"])}]
                sent = []
                block_ack = True
                block_commit = True

                async def pending_notice_feedback(self):
                    return [], clear, self.pending.copy()

                async def acknowledge_notice_reads(self, ids):
                    assert ids == [older_question["id"]]
                    assert shared.home_summary()["response_count"] == 1
                    if self.block_ack:
                        raise CommandRejected("low_battery_readonly")
                    self.pending.clear()

                async def sync_messages(self, records, **fields):
                    if self.block_commit:
                        raise CommandRejected("low_battery_readonly")
                    self.sent.append((records, fields))

            session.client = board = Device()
            await session.refresh_messages()
            self.assertEqual(len(board.pending), 1)
            self.assertEqual(len(board.sent), 0)
            board.block_ack = False
            await session.refresh_messages()
            self.assertEqual(board.pending, [])
            self.assertEqual(len(board.sent), 0)
            board.block_commit = False
            await session.refresh_messages()
            self.assertEqual(len(board.sent), 1)
            rows, fields = board.sent[-1]
            self.assertEqual(fields, {"summary": (1, 1), "clear": clear})
            self.assertEqual([row["id"] for row in rows if row["category"] == "response"], [latest["id"]])
            self.assertFalse(rows[0]["seen"])
            await session.refresh_messages()
            self.assertEqual(len(board.sent), 1)
            session.message_cache_due = 0
            await session.refresh_messages()
            self.assertEqual(len(board.sent), 2)
            session.notifications.resolve_response(latest["id"])
            await session.refresh_messages()
            self.assertEqual(board.sent[-1][1]["summary"], (1, 0))

    def test_screen_cache_prioritizes_pending_over_newer_completions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            questions = [store.add_agent_event("codex", f"s-{i}", "question", "response", f"request {i}")
                         for i in range(10)]
            store.add_agent_event("codex", "later", "done", "completed", "newer completion")
            rows, summary = store.legacy_message_snapshot(0, 0)
            self.assertEqual(summary, (1, 10))
            self.assertEqual([row["id"] for row in rows], list(reversed(questions)))
            self.assertTrue(all(row["category"] == "response" for row in rows))

    def test_existing_notifications_keep_their_delivery_state_after_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE notifications (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                    title TEXT NOT NULL, created REAL NOT NULL, shown REAL, seen REAL,
                    UNIQUE(source, session, turn))""")
                db.executemany("""INSERT INTO notifications (source, session, turn, title, created, shown)
                    VALUES ('codex', 's', ?, 'project', 1, ?)""", (("1", 2), ("2", None)))
            path.chmod(0o600)
            store = notifications.NotificationStore(path)
            self.assertEqual([item["shown"] for item in store.list()], [False, True])
            self.assertEqual(store.next_pending()["id"], 2)
            store.mark_shown(2)
            self.assertIsNone(store.next_pending())

    async def test_busy_action_retries_after_reopening_store_without_repeating_text(self):
        class Channel:
            capabilities = {"role_actions": 1}

        class Board:
            channel = Channel()
            actions_played = 0
            messages_shown = 0

            async def status(self):
                return {"role_state": "ready", "finish_action": "finish"}

            async def list_actions(self):
                return ["idle", "finish"]

            async def play_role(self, action, duration):
                self.actions_played += 1
                return {"action_status": "busy" if self.actions_played <= 2 else "started"}

            async def show_text(self, _text, *, category=None):
                self.messages_shown += 1
                return {"status": "shown"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            store = notifications.NotificationStore(path)
            identity = store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            board = Board()
            self.assertFalse(await notifications.deliver_next(board, store))
            self.assertTrue(store.list()[0]["shown"])
            store = notifications.NotificationStore(path)
            self.assertEqual(store.next_pending()["id"], identity)
            newer = store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t2"})
            self.assertFalse(await notifications.deliver_next(board, store))
            self.assertEqual([(row["id"], row["shown"]) for row in store.list()], [(newer, True), (identity, True)])
            self.assertEqual(store.next_pending()["id"], identity)
            self.assertEqual((board.actions_played, board.messages_shown), (2, 2))
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertEqual(store.next_pending()["id"], newer)
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertEqual((board.actions_played, board.messages_shown), (4, 2))
            self.assertIsNone(store.next_pending())

    async def test_unavailable_action_does_not_retry_while_delivering_text(self):
        class Channel:
            capabilities = {"role_actions": 1}

        class Board:
            channel = Channel()
            actions_played = 0
            messages_shown = 0

            async def status(self):
                return {"role_state": "ready", "finish_action": "finish"}

            async def list_actions(self):
                return ["idle", "finish"]

            async def play_role(self, action, duration):
                self.actions_played += 1
                return {"action_status": "unavailable", "reason": "music_active"}

            async def show_text(self, _text, *, category=None):
                self.messages_shown += 1
                return {"status": "shown"}

        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertEqual((board.actions_played, board.messages_shown), (1, 1))
            self.assertIsNone(store.next_pending())
            self.assertFalse(await notifications.deliver_next(board, store))
            self.assertEqual((board.actions_played, board.messages_shown), (1, 1))

    async def test_role_action_and_shown_receipt_are_used_without_private_content(self):
        class Channel:
            capabilities = {"role_actions": 1}
        class Board:
            channel = Channel()
            def __init__(self):
                self.action = self.text = None
            async def status(self):
                return {"role_state": "failed", "role_active_ready": True, "finish_action": "deliver_result"}
            async def list_actions(self):
                return ["idle", "deliver_result"]
            async def play_role(self, action, duration):
                self.action = (action, duration)
                return {"action_status": "started"}
            async def show_text(self, text, *, category=None):
                self.text = text
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t",
                                   "cwd": "/tmp/project", "last_assistant_message": "private reply"})
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertEqual(board.action, (1, 3000))
            self.assertIn("project", board.text)
            self.assertNotIn("private reply", board.text)
            self.assertIsNone(store.next_pending())

    async def test_unconfirmed_display_keeps_event_pending(self):
        class Channel:
            capabilities = {}
        class Board:
            channel = Channel()
            async def show_text(self, _text, *, category=None):
                return {"status": "display_unconfirmed"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            self.assertFalse(await notifications.deliver_next(Board(), store))
            self.assertIsNotNone(store.next_pending())

    async def test_role_can_declare_its_own_completion_action(self):
        class Channel:
            capabilities = {"role_actions": 1}
        class Board:
            channel = Channel()
            action = None
            async def status(self):
                return {"role_state": "ready", "finish_action": "dance_5"}
            async def list_actions(self):
                return ["idle", "dance_5"]
            async def play_role(self, action, duration):
                self.action = action
                return {"action_status": "started"}
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertEqual(board.action, 1)

    async def test_role_can_disable_its_completion_action(self):
        class Channel:
            capabilities = {"role_actions": 1}
        class Board:
            channel = Channel()
            action = None
            async def status(self):
                return {"role_state": "ready", "finish_action": ""}
            async def list_actions(self):
                return ["deliver_result"]
            async def play_role(self, action, duration):
                self.action = action
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertIsNone(board.action)

    async def test_missing_completion_mapping_does_not_guess_an_action(self):
        class Channel:
            capabilities = {"role_actions": 1}
        class Board:
            channel = Channel()
            action = None
            async def status(self):
                return {"role_state": "ready"}
            async def list_actions(self):
                return ["deliver_result"]
            async def play_role(self, action, duration):
                self.action = action
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
        with tempfile.TemporaryDirectory() as directory:
            store = notifications.NotificationStore(Path(directory) / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            board = Board()
            self.assertTrue(await notifications.deliver_next(board, store))
            self.assertIsNone(board.action)

    async def test_worker_delivers_with_a_synthetic_paired_board(self):
        class Channel:
            capabilities = {"text_present": 1}
        class Board:
            channel = Channel()
            def __init__(self, _config):
                self.count = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass
            async def set_notice_count(self, count):
                self.count = count
            async def status(self):
                return {"pet_page": 0}
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "device.json"
            DeviceConfig("02:00:00:00:00:01", bytes(range(32))).save(config)
            store = notifications.NotificationStore(config.parent / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            await notifications.run_worker(config, client_factory=Board, max_seconds=0.3, poll_interval=0.03)
            self.assertIsNone(store.next_pending())
            self.assertEqual(store.unseen_count(), 1)

    async def test_worker_notifies_independent_receiver_without_replacing_default(self):
        from deskpet_platform.device_registry import DeviceRegistry
        delivered=[]
        class Channel:
            capabilities={'text_present':1}
        class Board:
            channel=Channel()
            def __init__(self,config):self.identity=config.identity
            async def __aenter__(self):return self
            async def status(self):return {'pet_page':0}
            async def show_text(self, _text, *, category=None):delivered.append(self.identity);return {'status':'shown'}
            async def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'device.json'
            DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(config)
            registry=DeviceRegistry(config)
            receiver=registry.add_receiver(DeviceConfig('02:00:00:00:00:02',bytes(range(32))))
            store=notifications.NotificationStore(config.parent/'notifications.sqlite3')
            store.add_codex_event({'hook_event_name':'Stop','session_id':'s','turn_id':'t'})
            await asyncio.gather(notifications.run_worker(config,client_factory=Board,max_seconds=1.5,poll_interval=.03),
                                 notifications.run_receiver_worker(config,client_factory=Board,max_seconds=1.5,poll_interval=.03))
            self.assertEqual(set(delivered),{'02:00:00:00:00:01','02:00:00:00:00:02'})
            self.assertEqual(len(registry.list()),1)
            self.assertEqual(notifications.NotificationStore(receiver.parent/'notifications.sqlite3').unseen_count(),1)

    async def test_live_receiver_runs_data_sources_and_releases_them_on_handoff(self):
        from deskpet_platform.device_registry import DeviceRegistry
        started, stopped, instances = [], [], []
        class Session:
            def __init__(self, path, **kwargs):
                self.path, self.kwargs = path, kwargs
                self.client = None
                instances.append(self)
                started.append(('session', kwargs))
            async def drop(self): stopped.append('drop')
        def make_poll(name):
            async def run(self):
                started.append(name)
                try:
                    await asyncio.Future()
                finally:
                    stopped.append(name)
            return run
        for name in ('poll', 'poll_weather', 'poll_tokens', 'poll_quota', 'poll_quota_display', 'poll_antigravity_quota', 'poll_computer_metrics', 'poll_analytics'):
            setattr(Session, name, make_poll(name))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(config)
            receiver = DeviceRegistry(config).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            with patch('deskpet_platform.mcp_server.BoardSession', Session):
                task = asyncio.create_task(notifications.run_live_receiver(receiver, config, max_seconds=.55,
                                                                          poll_interval=.02, handoff_delay=.03))
                await asyncio.sleep(.08)
                handoff = receiver.parent / "notification-handoff"
                for _ in range(3):
                    self.assertTrue(notifications.request_worker_yield(receiver))
                    async with asyncio.timeout(.2):
                        while handoff.exists():
                            await asyncio.sleep(.01)
                    self.assertEqual(len(instances), 1)
                    self.assertEqual(stopped, [])
                instances[0].client = object()
                self.assertTrue(notifications.request_worker_yield(receiver))
                await task
            sessions = [item[1] for item in started if isinstance(item, tuple)]
            self.assertGreaterEqual(len(sessions), 2)
            self.assertEqual(sessions[0]['notification_source'].path, config.parent / 'notifications.sqlite3')
            self.assertEqual(sessions[0]['quota_store'].path, config.parent / 'agent-quota.sqlite3')
            for name in ('poll', 'poll_weather', 'poll_tokens', 'poll_quota', 'poll_quota_display', 'poll_antigravity_quota', 'poll_computer_metrics', 'poll_analytics'):
                self.assertGreaterEqual(started.count(name), 2)
                self.assertEqual(started.count(name), stopped.count(name))

    async def test_live_receiver_takes_over_from_notification_only_worker(self):
        from deskpet_platform.device_registry import DeviceRegistry
        started = []
        class Session:
            def __init__(self, path, **kwargs):
                self.live = 'quota_store' in kwargs
            async def poll(self):
                started.append('live' if self.live else 'fallback')
                await asyncio.Future()
            async def wait(self): await asyncio.Future()
            poll_weather = poll_tokens = poll_quota = poll_quota_display = poll_antigravity_quota = poll_computer_metrics = poll_analytics = wait
            async def drop(self): started.append('live closed' if self.live else 'fallback closed')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(config)
            receiver = DeviceRegistry(config).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            with patch('deskpet_platform.mcp_server.BoardSession', Session):
                fallback = asyncio.create_task(notifications._run_device_worker(
                    receiver, source=notifications.NotificationStore(config.parent/'notifications.sqlite3'),
                    client_factory=None, deadline=time.monotonic()+2, poll_interval=.02, handoff_delay=.01))
                await asyncio.sleep(.08)
                self.assertIn('fallback', started)
                live = asyncio.create_task(notifications.run_live_receiver(receiver, config, max_seconds=1.5,
                                                                           poll_interval=.02))
                await asyncio.gather(fallback, live)
            self.assertIn('fallback closed', started)
            self.assertIn('live', started)
            self.assertLess(started.index('fallback closed'), started.index('live'))

    async def test_notification_worker_resumes_after_live_receiver_stops(self):
        from deskpet_platform.device_registry import DeviceRegistry
        started = []
        class Session:
            def __init__(self, path, **kwargs): self.live = 'quota_store' in kwargs
            async def poll(self):
                started.append('live' if self.live else 'fallback')
                await asyncio.Future()
            async def wait(self): await asyncio.Future()
            poll_weather = poll_tokens = poll_quota = poll_quota_display = poll_antigravity_quota = poll_computer_metrics = poll_analytics = wait
            async def drop(self): pass
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(config)
            receiver = DeviceRegistry(config).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            source = notifications.NotificationStore(config.parent/'notifications.sqlite3')
            with patch('deskpet_platform.mcp_server.BoardSession', Session):
                live = asyncio.create_task(notifications.run_live_receiver(receiver, config, max_seconds=.25,
                                                                           poll_interval=.02))
                await asyncio.sleep(.08)
                await notifications._run_device_worker(receiver, source=source, client_factory=None,
                    deadline=time.monotonic()+.1, poll_interval=.02, handoff_delay=.01)
                self.assertNotIn('fallback', started)
                await live
                await notifications._run_device_worker(receiver, source=source, client_factory=None,
                    deadline=time.monotonic()+.1, poll_interval=.02, handoff_delay=.01)
            self.assertIn('live', started)
            self.assertIn('fallback', started)

    async def test_live_receiver_rejects_unregistered_device(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / 'device.json'
            other = Path(directory) / 'other.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(shared)
            DeviceConfig('02:00:00:00:00:02', bytes(range(32))).save(other)
            with self.assertRaises(ValueError):
                await notifications.run_live_receiver(other, shared, max_seconds=.1)

    async def test_live_receiver_discovers_windows_probe_from_shared_config(self):
        from deskpet_platform.device_registry import DeviceRegistry
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(shared)
            receiver = DeviceRegistry(shared).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            probe = root / 'bin' / 'AgentSpeedProbe.exe'
            probe.parent.mkdir()
            probe.touch()
            with patch('deskpet_platform.agent_speed_probe.platform', 'win32'):
                with patch('deskpet_platform.notifications._run_device_worker', new_callable=AsyncMock) as run:
                    await notifications.run_live_receiver(receiver, shared)
            self.assertEqual(run.await_args.kwargs['live_options']['token_probe'], probe)

    async def test_live_receiver_has_no_implicit_daily_expiry(self):
        from deskpet_platform.device_registry import DeviceRegistry
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(shared)
            receiver = DeviceRegistry(shared).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            with patch('deskpet_platform.notifications._run_device_worker',new_callable=AsyncMock) as run:
                await notifications.run_live_receiver(receiver,shared)
            self.assertEqual(run.await_args.kwargs['deadline'],float('inf'))

    async def test_live_receiver_surfaces_failed_data_task_and_closes_session(self):
        from deskpet_platform.device_registry import DeviceRegistry
        closed = []
        class Session:
            def __init__(self, *args, **kwargs): pass
            async def poll(self):
                await asyncio.sleep(.01)
                raise RuntimeError('data task failed')
            async def wait(self): await asyncio.Future()
            poll_weather = poll_tokens = poll_quota = poll_quota_display = poll_antigravity_quota = poll_computer_metrics = poll_analytics = wait
            async def drop(self): closed.append(True)
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / 'device.json'
            DeviceConfig('02:00:00:00:00:01', bytes(range(32))).save(shared)
            receiver = DeviceRegistry(shared).add_receiver(DeviceConfig('02:00:00:00:00:02', bytes(range(32))))
            with patch('deskpet_platform.mcp_server.BoardSession', Session):
                with self.assertRaisesRegex(RuntimeError, 'data task failed'):
                    await notifications.run_live_receiver(receiver, shared, max_seconds=1, poll_interval=.02)
            self.assertEqual(closed, [True])

    async def test_receiver_worker_runs_while_existing_dispatcher_is_locked(self):
        from deskpet_platform.device_registry import DeviceRegistry
        from deskpet_platform.file_lock import acquire, release
        delivered=[]
        class Channel:
            capabilities={'text_present':1}
        class Board:
            channel=Channel()
            def __init__(self,config):self.identity=config.identity
            async def __aenter__(self):return self
            async def status(self):return {'pet_page':0}
            async def show_text(self, _text, *, category=None):delivered.append(self.identity);return {'status':'shown'}
            async def close(self):pass
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'device.json'
            DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(config)
            DeviceRegistry(config).add_receiver(DeviceConfig('02:00:00:00:00:02',bytes(range(32))))
            notifications.NotificationStore(config.parent/'notifications.sqlite3').add_codex_event(
                {'hook_event_name':'Stop','session_id':'s','turn_id':'t'})
            fd=os.open(config.parent/'notification-dispatch.lock',os.O_CREAT|os.O_RDWR,0o600)
            try:
                acquire(fd,blocking=False)
                await notifications.run_receiver_worker(config,client_factory=Board,max_seconds=1.5,poll_interval=.03)
            finally:
                release(fd);os.close(fd)
            self.assertEqual(delivered,['02:00:00:00:00:02'])

    async def test_worker_releases_board_when_foreground_requests_it(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        class Channel:
            capabilities = {}
        class Board:
            channel = Channel()
            def __init__(self, _config):
                pass
            async def __aenter__(self):
                entered.set()
                return self
            async def status(self):
                return {}
            async def show_text(self, _text, *, category=None):
                return {"status": "shown"}
            async def close(self):
                closed.set()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "device.json"
            DeviceConfig("02:00:00:00:00:01", bytes(range(32))).save(config)
            store = notifications.NotificationStore(config.parent / "notifications.sqlite3")
            store.add_codex_event({"hook_event_name": "Stop", "session_id": "s", "turn_id": "t"})
            task = asyncio.create_task(notifications.run_worker(
                config, client_factory=Board, max_seconds=1, poll_interval=0.03, handoff_delay=0.1))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertTrue(notifications.request_worker_yield(config))
                await asyncio.wait_for(closed.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
