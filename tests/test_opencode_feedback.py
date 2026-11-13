import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from deskpet_platform.notifications import NotificationStore
from deskpet_platform.opencode_feedback import configure, deliver


class OpenCodeFeedbackTests(unittest.TestCase):
    def test_install_uses_argv_and_preserves_other_plugins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugins" / "deskpet-test.js"
            command = shlex.join([sys.executable, "/some dir/with 'quote'.py"])
            self.assertTrue(configure(path, command, enabled=True))
            other = path.with_name("other.js")
            other.write_text("export const Other = () => ({})")
            self.assertIn(json.dumps(shlex.split(command)), path.read_text())
            self.assertFalse(configure(path, command, enabled=True))
            self.assertTrue(configure(path, command, enabled=False))
            self.assertTrue(other.exists())
            self.assertFalse(configure(path, command, enabled=False))
            path.write_text("// external file")
            with self.assertRaisesRegex(ValueError, "同名"):
                configure(path, command, enabled=True)
            self.assertEqual(path.read_text(), "// external file")

    def test_exact_request_cleared_even_when_reply_arrives_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStore(Path(directory) / "notifications.sqlite3")
            event = {"session": "ses_1", "id": "req_1", "request": "question"}
            self.assertFalse(deliver(store, {**event, "kind": "resolved"}))
            self.assertTrue(deliver(store, {**event, "kind": "asked", "title": "Choice"}))
            self.assertTrue(deliver(store, {**event, "kind": "asked", "request": "permission"}))
            items = store.list()
            self.assertEqual([(row["source"], row["category"], row["resolved"]) for row in items],
                             [("opencode", "response", False), ("opencode", "response", True)])
            self.assertFalse(deliver(store, {"kind": "completed", "id": 123, "session": "ses_1"}))

    @unittest.skipUnless(shutil.which("bun"), "Bun is needed to run the OpenCode plugin")
    def test_plugin_routes_structured_events_and_ignores_failed_or_incomplete_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "events.jsonl"
            command = shlex.join([sys.executable, "-c", "import sys;open(sys.argv[1],'a').write(sys.stdin.read()+'\\n')", str(log)])
            path = root / "deskpet.js"
            configure(path, command, enabled=True)
            fixture = root / "fixture.js"
            fixture.write_text("""
import { DeskPetNotifications } from "./deskpet.js";
const plugin = await DeskPetNotifications({ directory: "/fixtures/project" });
const send = (type, properties) => plugin.event({ event: { type, properties } });
const msg = (sessionID, id, finish, rest = {}) =>
  send("message.updated", { info: { sessionID, id, role: "assistant", finish, time: { completed: 2 }, ...rest } });
const idle = (sessionID) => send("session.status", { sessionID, status: { type: "idle" } });
await msg("failed", "msg_0", "stop", { error: { name: "APIError" } });
await idle("failed");
await msg("tools", "msg_1", "tool-calls");
await idle("tools");
await msg("success", "msg_2", "stop");
await idle("success");
await idle("success");
await send("question.asked", { id: "q", sessionID: "s", questions: [{ header: "Target", question: "private body" }] });
await send("permission.asked", { id: "p", sessionID: "s", permission: "write", patterns: ["private value"] });
await send("question.rejected", { requestID: "q", sessionID: "s" });
await send("permission.replied", { requestID: "p", sessionID: "s", reply: "once" });
""", encoding="utf-8")
            subprocess.run(["bun", str(fixture)], check=True, timeout=15)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(events, [
                {"kind": "completed", "session": "success", "id": "msg_2", "title": "project"},
                {"kind": "asked", "session": "s", "id": "q", "request": "question", "title": "Target"},
                {"kind": "asked", "session": "s", "id": "p", "request": "permission", "title": "write"},
                {"kind": "resolved", "session": "s", "id": "q", "request": "question"},
                {"kind": "resolved", "session": "s", "id": "p", "request": "permission"},
            ])
