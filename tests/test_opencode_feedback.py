import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_initial_install_preserves_a_file_that_appears_during_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deskpet-test.js"

            def concurrent_install(staged, target):
                target.write_bytes(staged.read_bytes())
                raise FileExistsError()

            with patch("deskpet_platform.opencode_feedback.os.link", side_effect=concurrent_install):
                self.assertFalse(configure(path, "deskpet hook", enabled=True))
            self.assertFalse(configure(path, "deskpet hook", enabled=True))

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

    def test_refresh_or_remove_only_an_unmodified_previous_plugin_for_this_command(self):
        marker = b"/* DESKPET_COMMAND */ []"
        previous = b"const command = " + marker + b";\n// Shipped plugin example\n"
        command = shlex.join([sys.executable, "/a path/with 'quotes'.py"])
        installed = previous.replace(marker, json.dumps(shlex.split(command)).encode())
        with (tempfile.TemporaryDirectory() as directory,
              patch("deskpet_platform.opencode_feedback.PREVIOUS_PLUGIN_SHA256",
                    hashlib.sha256(previous).hexdigest())):
            path = Path(directory) / "deskpet-test.js"
            path.write_bytes(installed)
            self.assertTrue(configure(path, command, enabled=True))
            self.assertNotEqual(path.read_bytes(), installed)
            self.assertFalse(configure(path, command, enabled=True))

            path.write_bytes(installed)
            self.assertTrue(configure(path, command, enabled=False))
            self.assertFalse(path.exists())
            for foreign in (installed + b"// Custom edit\n", previous.replace(marker, b'["different"]')):
                path.write_bytes(foreign)
                for enabled in (True, False):
                    with self.subTest(foreign=foreign, enabled=enabled), self.assertRaisesRegex(ValueError, "同名"):
                        configure(path, command, enabled=enabled)
                    self.assertEqual(path.read_bytes(), foreign)
            path.unlink()
            external = path.with_name("other.js")
            external.write_bytes(installed)
            path.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "同名"):
                configure(path, command, enabled=True)
            self.assertEqual(external.read_bytes(), installed)

    def test_session_end_resolves_only_existing_opencode_requests_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = NotificationStore(root / "notifications.sqlite3")
            board = NotificationStore(root / "board/notifications.sqlite3", source=store)
            session = {"session": "ses_ended"}
            deliver(store, {**session, "kind": "completed", "id": "previous"})
            deliver(store, {**session, "kind": "asked", "request": "question", "id": "one", "title": "Question"})
            deliver(store, {**session, "kind": "asked", "request": "permission", "id": "two", "title": "Approval"})
            deliver(store, {"session": "ses_open", "kind": "asked", "request": "question", "id": "three",
                            "title": "Other session"})
            store.add_agent_event("claude_code", "ses_ended", "four", "response", "Other agent")
            board.sync()
            board.mark_seen(next(item["id"] for item in board.list() if item["title"] == "Question"))

            self.assertFalse(deliver(store, {"kind": "session-ended", "session": ""}))
            self.assertTrue(deliver(store, {**session, "kind": "session-ended"}))
            self.assertFalse(deliver(store, {**session, "kind": "session-ended"}))
            self.assertTrue(deliver(store, {**session, "kind": "asked", "request": "question", "id": "new",
                                            "title": "New question"}))
            board.sync()
            self.assertEqual([(item["title"], item["seen"], item["resolved"]) for item in board.list()], [
                ("New question", False, False), ("Other agent", False, False), ("Other session", False, False),
                ("Approval", False, True), ("Question", True, True), ("OpenCode", False, False),
            ])

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
await send("session.error", { sessionID: "s", error: { name: "ProviderAuthError" } });
await idle("s");
await send("session.error", { sessionID: "aborted", error: { name: "MessageAbortedError", data: { message: "private" } } });
await send("session.deleted", { info: { id: "deleted", title: "private title" } });
""", encoding="utf-8")
            subprocess.run(["bun", str(fixture)], check=True, timeout=15)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(events, [
                {"kind": "completed", "session": "success", "id": "msg_2", "title": "project"},
                {"kind": "asked", "session": "s", "id": "q", "request": "question", "title": "Target"},
                {"kind": "asked", "session": "s", "id": "p", "request": "permission", "title": "write"},
                {"kind": "resolved", "session": "s", "id": "q", "request": "question"},
                {"kind": "resolved", "session": "s", "id": "p", "request": "permission"},
                {"kind": "session-ended", "session": "aborted"},
                {"kind": "session-ended", "session": "deleted"},
            ])
