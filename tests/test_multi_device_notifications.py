import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from deskpet_platform.client import Client
from deskpet_platform.codex_hook import hook_command, install_hook, receive
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.mcp_server import create_server, require_notification_hooks
from deskpet_platform.notifications import NotificationStore, run_worker
from test_multi_device_flow import FIRST, SECOND, SyntheticBoard


class NotificationPlatformTests(unittest.TestCase):
    def test_windows_allows_only_verified_claude_hook(self):
        require_notification_hooks(True, "nt", "claude-hook")
        with self.assertRaisesRegex(ValueError, "Windows"):
            require_notification_hooks(True, "nt", "codex-hook")
    def test_windows_install_is_refused_before_unverified_hooks_are_written(self):
        with self.assertRaisesRegex(ValueError, "Windows"):
            require_notification_hooks(True, "nt")
        require_notification_hooks(False, "nt")
        require_notification_hooks(True, "posix")


class NotificationBoard(SyntheticBoard):
    wireless = True

    def __init__(self, config):
        super().__init__(config)
        self.available = True
        self.snapshots = []
        self.pending = None

    async def open(self):
        if not self.available:
            raise OSError("offline")

    async def write(self, data):
        if not self.available:
            raise OSError("offline")
        request = json.loads(data[3:])
        kind = request["type"]
        response = {key: request[key] for key in ("epoch", "seq")}
        response.update(boot=1, ok=True)
        if kind == "hello":
            response.update(mac=self.config.identity, protocol=1, rx_bytes=2048,
                            device_id=self.config.device_id, brightness_control=1,
                            agent_home=1, response_attention=1, compact_status=1)
        elif kind == "heartbeat":
            response.update(role_state="ready", pet_page=0)
        elif kind == "snapshot_begin":
            self.pending = request
        elif kind == "row":
            assert self.pending["version"] == request["version"]
        elif kind == "snapshot_commit":
            assert self.pending["version"] == request["version"]
            self.snapshots.append(self.pending)
            response["committed_version"] = request["version"]
        elif kind == "brightness_set":
            self.brightness.append(request["level"])
            response["brightness"] = request["level"]
        else:
            raise AssertionError(kind)
        await self.response.put(b"P1," + json.dumps(response).encode() + b"\n")

    @property
    def attention(self):
        return [item["response_attention"] for item in self.snapshots if "response_attention" in item]


class MultiDeviceNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-profile")}))
        self.config = self.root / "device.json"
        FIRST.save(self.config)
        registry = DeviceRegistry(self.config)
        registry.add(SECOND)
        self.second_path = registry.path_for(SECOND.device_id)
        self.inbox = NotificationStore(self.root / "notifications.sqlite3")
        self.second = NotificationStore(self.second_path.parent / "notifications.sqlite3", source=self.inbox)

    async def wait_for(self, condition, timeout=8):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(.05)

    def submit(self, event, config=None):
        with patch("sys.stdin", io.StringIO(json.dumps(event))), redirect_stdout(io.StringIO()), \
             patch("deskpet_platform.codex_hook.subprocess.Popen") as spawn:
            receive(config or self.config)
        return spawn

    async def test_windows_notification_setup_does_not_touch_user_settings(self):
        server = create_server(self.config, no_token_probe=True)
        with patch("deskpet_platform.mcp_server.require_notification_hooks",
                   side_effect=lambda enabled, platform, hook=None: require_notification_hooks(enabled, "nt", hook)):
            for name, arguments in (("desk_enable_codex_notifications", {}),
                                    ("desk_set_antigravity_notifications", {"enabled": True})):
                with self.subTest(name=name), self.assertRaisesRegex(ToolError, "Windows"):
                    await server.call_tool(name, arguments)
        self.assertFalse((self.root / "codex-profile" / "hooks.json").exists())

    async def test_codex_hook_routes_to_both_boards_and_retries_offline_inbox(self):
        boards = {item.identity: NotificationBoard(item) for item in (FIRST, SECOND)}
        first, second = boards[FIRST.identity], boards[SECOND.identity]
        stop = {"hook_event_name": "Stop", "session_id": "test", "turn_id": "complete", "cwd": "/example"}
        self.submit(stop, self.second_path).assert_called_once()
        self.submit(stop).assert_called_once()  # Redelivery starts no new inbox record.
        self.assertEqual(len(self.inbox.list()), 1)
        self.assertEqual(self.second.list(), [])
        question = {
            "hook_event_name": "PreToolUse", "session_id": "test", "turn_id": "first-question", "cwd": "/example",
            "tool_name": "request_user_input_async", "tool_use_id": "call_online",
            "tool_input": {"questions": [{"title": "Private question"}]},
        }
        self.submit(question).assert_called_once()

        def factory(config):
            return Client(config, link_factory=boards[config.identity].link)

        task = asyncio.create_task(run_worker(self.config, client_factory=factory, poll_interval=.05))
        try:
            await self.wait_for(lambda: all(len(board.attention) == 1 for board in boards.values()))
            self.assertEqual([(board.snapshots[-1]["results"], board.snapshots[-1]["waiting"])
                              for board in boards.values()], [(1, 1), (1, 1)])
            await self.wait_for(lambda: all(record["shown"] for store in (self.inbox, self.second)
                                            for record in store.list()))
            first_id = next(record["id"] for record in self.inbox.list() if record["category"] == "completed")
            self.inbox.mark_seen(first_id)
            second_completed = next(record for record in self.second.list() if record["category"] == "completed")
            self.assertFalse(second_completed["seen"])
            async with Client(FIRST, link_factory=first.link) as manual:
                await manual.set_brightness(30)
            second.available = False
            offline_question = {**question, "turn_id": "offline-question", "tool_use_id": "call_offline"}
            offline_stop = {**stop, "turn_id": "offline-completion"}
            self.submit(offline_question).assert_called_once()
            self.submit({**stop, "turn_id": "offline-question"}).assert_not_called()
            self.submit(offline_stop).assert_called_once()
            await self.wait_for(lambda: len(first.attention) == 2 and first.snapshots[-1]["results"] == 1)
            self.assertEqual(len(second.attention), 1)
            self.assertEqual(second.snapshots[-1]["results"], 1)
            await self.wait_for(lambda: len(self.second.list()) == 4)
            self.assertCountEqual([record["category"] for record in self.second.list() if not record["shown"]],
                                  ["completed", "response"])
            second.available = True
            await self.wait_for(lambda: len(second.attention) == 2 and second.snapshots[-1]["results"] == 1)
            await self.wait_for(lambda: all(record["shown"] for record in self.second.list()))
            async with Client(SECOND, link_factory=second.link) as manual:
                await manual.set_brightness(80)
            self.assertEqual((first.brightness, second.brightness), ([30], [80]))
            self.submit(offline_question, self.second_path).assert_called_once()
            self.submit(offline_stop, self.second_path).assert_called_once()
            answer = {
                "hook_event_name": "UserPromptSubmit", "session_id": "test",
                "prompt": "<send_user_message_question_reply>\n" + json.dumps([{
                    "questionItemId": json.dumps(["request_user_input_async", "call_offline", 0]),
                    "answer": "Private answer",
                }]) + "\n</send_user_message_question_reply>",
            }
            self.submit(answer).assert_called_once()
            self.submit(answer).assert_not_called()
            await self.wait_for(lambda: all(board.snapshots[-1]["waiting"] == 1 for board in boards.values()))
            self.assertEqual([len(board.attention) for board in boards.values()], [2, 2])
            self.assertEqual([board.snapshots[-1]["results"] for board in boards.values()], [1, 1])

            post_tool = {**question, "hook_event_name": "PostToolUse"}
            self.submit({**post_tool, "tool_response": '{"accepted": true}'}).assert_not_called()
            self.submit({**post_tool, "tool_response": '{"accepted": false}'}).assert_called_once()
            self.submit({**post_tool, "tool_response": '{"accepted": false}'}).assert_not_called()
            await self.wait_for(lambda: all(board.snapshots[-1]["waiting"] == 0 for board in boards.values()))
            for store in (self.inbox, self.second):
                responses = [record for record in store.list() if record["category"] == "response"]
                self.assertEqual(len(responses), 2)
                self.assertTrue(all(record["shown"] and record["resolved"] for record in responses))

            self.submit({**stop, "session_id": "other-session", "turn_id": "after-manual-control"})
            await self.wait_for(lambda: all(board.snapshots[-1]["results"] == 2 for board in boards.values()))
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_stop_arriving_during_dispatcher_shutdown_reaches_both_boards(self):
        closing = asyncio.Event()
        allow_close = asyncio.Event()

        class ClosingBoard(NotificationBoard):
            async def close(self):
                closing.set()
                await allow_close.wait()

        boards = {FIRST.identity: ClosingBoard(FIRST), SECOND.identity: NotificationBoard(SECOND)}

        def factory(config):
            return Client(config, link_factory=boards[config.identity].link)

        options = dict(client_factory=factory, poll_interval=.01, idle_seconds=.02, max_seconds=2)
        previous = asyncio.create_task(run_worker(self.config, **options))
        contender = None
        try:
            await asyncio.wait_for(closing.wait(), 1)
            stop = {"hook_event_name": "Stop", "session_id": "test", "turn_id": "at-idle-exit"}
            self.submit(stop).assert_called_once()
            # The process launched by the hook reaches the real dispatch lock
            # while the previous process is still shutting down its boards.
            contender = asyncio.create_task(run_worker(self.config, **options))
            second = boards[SECOND.identity]
            await self.wait_for(lambda: second.snapshots and second.snapshots[-1]["results"] == 1, timeout=1.5)
            self.assertFalse(previous.done())
            self.assertEqual(boards[FIRST.identity].snapshots[-1]["results"], 0)
            allow_close.set()
            await self.wait_for(lambda: boards[FIRST.identity].snapshots[-1]["results"] == 1, timeout=1.5)
            for store in (self.inbox, self.second):
                self.assertEqual(len(store.list()), 1)
        finally:
            allow_close.set()
            tasks = [task for task in (previous, contender) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_stop_between_idle_scan_and_unlock_stays_with_the_dispatcher(self):
        from deskpet_platform import notifications

        boards = {item.identity: NotificationBoard(item) for item in (FIRST, SECOND)}

        def factory(config):
            return Client(config, link_factory=boards[config.identity].link)

        stop = {"hook_event_name": "Stop", "session_id": "test", "turn_id": "during-idle-scan"}
        release = notifications._release_idle_dispatch
        scanned = asyncio.Event()

        def finish_idle_scan(*args):
            # This hook commits after both inactivity checks. A contender
            # would see the still-held lock and leave before we can unlock.
            if not scanned.is_set():
                self.submit(stop).assert_called_once()
                scanned.set()
            return release(*args)

        with patch.object(notifications, "_release_idle_dispatch", side_effect=finish_idle_scan):
            task = asyncio.create_task(run_worker(self.config, client_factory=factory, poll_interval=.01,
                                                  idle_seconds=.02, max_seconds=2))
            try:
                await asyncio.wait_for(scanned.wait(), 1)
                await self.wait_for(lambda: all(board.snapshots and board.snapshots[-1]["results"] == 1
                                                    for board in boards.values()), timeout=1.5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_hook_installation_is_shared_by_all_manual_selections(self):
        path = self.root / "codex-profile/hooks.json"
        default_path = self.root / ".codex/hooks.json"
        install_hook(default_path, "default profile hook")
        default_content = default_path.read_bytes()
        legacy = hook_command(self.second_path)
        install_hook(path, legacy)
        install_hook(path, "unrelated hook")
        with (patch("pathlib.Path.home", return_value=self.root),
              patch.dict(os.environ, {"CODEX_HOME": str(path.parent)})):
            server = create_server(self.config)
            await server.call_tool("desk_enable_codex_notifications", {"device_id": SECOND.device_id})
            await server.call_tool("desk_enable_codex_notifications", {"device_id": FIRST.device_id})
            await server.call_tool("desk_enable_codex_notifications", {})
            groups = json.loads(path.read_text())["hooks"]["Stop"]
            self.assertEqual([hook["command"] for group in groups for hook in group["hooks"]],
                             ["unrelated hook", hook_command(self.config)])
            await server.call_tool("desk_disable_codex_notifications", {"device_id": SECOND.device_id})
            groups = json.loads(path.read_text())["hooks"]["Stop"]
            self.assertEqual([hook["command"] for group in groups for hook in group["hooks"]], ["unrelated hook"])
        self.assertEqual(default_path.read_bytes(), default_content)

    async def test_codex_hook_configuration_leaves_file_unchanged_if_legacy_removal_is_unsafe(self):
        path = Path(os.environ["CODEX_HOME"]) / "hooks.json"
        legacy = hook_command(self.second_path)
        current = hook_command(self.config)
        install_hook(path, legacy)
        config = json.loads(path.read_text())
        config["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "unrelated hook"})
        path.write_text(json.dumps(config))
        server = create_server(self.config, no_token_probe=True)

        before = path.read_bytes()
        with self.assertRaisesRegex(ToolError, "其他同组 hook"):
            await server.call_tool("desk_enable_codex_notifications", {"device_id": SECOND.device_id})
        self.assertEqual(path.read_bytes(), before)

        install_hook(path, current)
        before = path.read_bytes()
        with self.assertRaisesRegex(ToolError, "其他同组 hook"):
            await server.call_tool("desk_disable_codex_notifications", {"device_id": FIRST.device_id})
        self.assertEqual(path.read_bytes(), before)

    async def test_failed_home_commit_does_not_record_delivery_or_attention(self):
        from deskpet_platform.notification_view import NotificationView
        from test_notification_view import FakeClient

        identity = self.inbox.add_agent_event("codex", "s", "ask", "response", "Question")
        client = FakeClient()
        client.channel.capabilities["response_attention"] = 1
        client.channel.failure = ("snapshot_commit", None)
        view = NotificationView(self.inbox)
        with self.assertRaises(OSError):
            await view.refresh(client)
        self.assertFalse(self.inbox.list()[0]["shown"])
        self.assertEqual(self.inbox.pending_attention(), identity)
        client.channel.failure = None
        await view.refresh(client)
        self.assertTrue(self.inbox.list()[0]["shown"])
        self.assertEqual(self.inbox.pending_attention(), 0)

    def test_sync_reuses_existing_records_and_resolves_old_or_delayed_events(self):
        old = self.second.add_agent_event("codex", "s", "existing", "response", "Old")
        self.second.mark_seen(old)
        self.second.mark_shown(old)
        self.inbox.add_agent_event("codex", "s", "existing", "response", "New title")
        self.inbox.resolve_agent_event("codex", "s", "existing")
        self.inbox.resolve_agent_event("codex", "s", "late")
        self.second.sync()
        self.inbox.add_agent_event("codex", "s", "late", "response", "Late")
        self.second.sync()
        self.second.sync()
        late, previous = self.second.list()
        self.assertEqual(previous["id"],old)
        self.assertTrue(previous["seen"] and previous["shown"] and previous["resolved"])
        self.assertTrue(late["resolved"])
        self.assertFalse(late["shown"] or late["seen"])
