import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.client import Client
from deskpet_platform.codex_hook import hook_command, install_hook, receive
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.mcp_server import create_server
from deskpet_platform.notifications import NotificationStore, run_worker
from test_multi_device_flow import FIRST, SECOND, SyntheticBoard


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

    def submit(self, event, config=None, handler=None):
        with patch("sys.stdin", io.StringIO(json.dumps(event))), redirect_stdout(io.StringIO()), \
             patch("deskpet_platform.codex_hook.subprocess.Popen") as spawn:
            receive(config or self.config, handle=handler)
        return spawn

    async def test_background_routes_to_both_boards_and_retries_offline_inbox(self):
        from deskpet_platform.opencode_feedback import deliver

        boards = {item.identity: NotificationBoard(item) for item in (FIRST, SECOND)}
        first, second = boards[FIRST.identity], boards[SECOND.identity]
        stop = {"hook_event_name": "Stop", "session_id": "test", "turn_id": "complete", "cwd": "/example"}
        self.submit(stop, self.second_path).assert_called_once()
        self.submit(stop).assert_called_once()  # Redelivery starts no new inbox record.
        self.assertEqual(len(self.inbox.list()), 1)
        self.assertEqual(self.second.list(), [])
        question = {"session": "test", "id": "ask", "kind": "asked", "request": "question", "title": "Question"}
        self.submit(question, handler=deliver).assert_called_once()

        def factory(config):
            return Client(config, link_factory=boards[config.identity].link)

        task = asyncio.create_task(run_worker(self.config, client_factory=factory, poll_interval=.05))
        try:
            await self.wait_for(lambda: all(len(board.attention) == 1 for board in boards.values()))
            self.assertEqual([(board.snapshots[-1]["results"], board.snapshots[-1]["waiting"])
                              for board in boards.values()], [(1,1),(1,1)])
            self.assertTrue(all(record["shown"] for store in (self.inbox,self.second) for record in store.list()))
            first_id = next(record["id"] for record in self.inbox.list() if record["category"] == "completed")
            self.inbox.mark_seen(first_id)
            async with Client(FIRST, link_factory=first.link) as manual:
                await manual.set_brightness(30)
            second.available = False
            question = {**question, "id": "offline"}
            self.submit(question, handler=deliver)
            await self.wait_for(lambda: len(first.attention) == 2)
            self.assertEqual(len(second.attention),1)
            self.assertTrue(any(record["category"] == "response" and not record["shown"] for record in self.second.list()))
            second.available = True
            await self.wait_for(lambda: len(second.attention) == 2)
            self.assertEqual(second.snapshots[-1]["results"], 1)
            self.assertEqual(first.snapshots[-1]["results"], 0)
            self.assertEqual(len(self.second.list()), 3)
            self.assertTrue(all(record["shown"] for record in self.second.list()))
            async with Client(SECOND, link_factory=second.link) as manual:
                await manual.set_brightness(80)
            self.assertEqual((first.brightness, second.brightness), ([30], [80]))
            self.submit(question, handler=deliver)
            await asyncio.sleep(1.2)
            self.assertEqual([len(board.attention) for board in (first,second)], [2,2])
            self.submit({**question,"kind":"resolved"},handler=deliver)
            await self.wait_for(lambda: all(board.snapshots[-1]["waiting"] == 1 for board in (first,second)))
            self.submit({**stop, "turn_id": "after-manual-control"})
            await self.wait_for(lambda: first.snapshots[-1]["results"] == 1 and second.snapshots[-1]["results"] == 2)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

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
