import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import antigravity_feedback, codex_hook
from deskpet_platform.notifications import NotificationStore


class AntigravityFeedbackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = NotificationStore(self.root / "notifications.sqlite3")

    def test_completed_stop_enters_the_store_and_other_stops_do_not(self):
        event = {"terminationReason": "model_stop", "fullyIdle": True, "error": "",
                 "conversationId": "conversation", "executionNum": 1, "workspacePaths": ["/sample/project"],
                 "lastUserInput": "private input"}
        config = self.root / "device.json"
        command = [sys.executable, "-m", "deskpet_platform", "--config", str(config), "antigravity-hook"]
        for change in ({"fullyIdle": False}, {"terminationReason": "error"}, {}, {}):
            result = subprocess.run(command, input=json.dumps({**event, **change}), capture_output=True,
                                    text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {"decision": "stop"})
        records = self.store.list()
        self.assertEqual([(row["source"], row["title"], row["category"]) for row in records],
                         [("antigravity", "project", "completed")])
        self.assertNotIn(b"private input", self.store.path.read_bytes())

    def test_named_hook_entries_can_target_two_boards_independently(self):
        path = self.root / "hooks.json"
        existing = {"other": {"Stop": [{"type": "command", "command": "existing"}]}}
        path.write_text(json.dumps(existing))
        first = codex_hook.hook_command(self.root / "first.json", "antigravity-hook")
        second = codex_hook.hook_command(self.root / "second.json", "antigravity-hook")
        self.assertTrue(antigravity_feedback.configure(path, first, enabled=True))
        self.assertFalse(antigravity_feedback.configure(path, first, enabled=True))
        self.assertTrue(antigravity_feedback.configure(path, second, enabled=True))
        self.assertTrue(antigravity_feedback.configure(path, first, enabled=False))
        installed = json.loads(path.read_text())
        self.assertEqual(len(installed), 2)
        self.assertEqual(installed["other"], existing["other"])
        self.assertTrue(antigravity_feedback.configure(path, second, enabled=False))
        self.assertEqual(json.loads(path.read_text()), existing)

    def test_native_stop_counts_two_turns_with_the_same_execution_number(self):
        session = "e93c9280-55ee-488a-84e9-033f36cb50c0"
        transcript = self.root / ".gemini" / "antigravity-cli" / "brain" / session
        transcript = transcript / ".system_generated" / "logs" / "transcript_full.jsonl"
        transcript.parent.mkdir(parents=True)
        event = {"hookEventName": "Stop", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
                 "conversationId": session, "executionNum": 0, "transcriptPath": str(transcript),
                 "workspacePaths": ["/sample/project"], "lastUserInput": "private input"}

        def write_step(index, source, kind):
            with transcript.open("a") as stream:
                stream.write(json.dumps({"step_index": index, "source": source, "type": kind,
                                         "status": "DONE", "content": "private transcript"}) + "\n")

        with patch("deskpet_platform.antigravity_feedback.Path.home", return_value=self.root):
            self.assertFalse(antigravity_feedback.deliver(self.store, event))
            for index in (0, 3):
                write_step(index, "USER_EXPLICIT", "USER_INPUT")
                self.assertFalse(antigravity_feedback.deliver(self.store, event))
                write_step(index + 1, "SYSTEM", "SYSTEM_MESSAGE")
                write_step(index + 2, "MODEL", "PLANNER_RESPONSE")
                for change in ({"fullyIdle": False}, {"continuationActive": True}, {"error": "failed"},
                               {"terminationReason": "WAITING_FOR_USER"}, {"hookEventName": "SessionStart"}):
                    self.assertFalse(antigravity_feedback.deliver(self.store, {**event, **change}))
                self.assertTrue(antigravity_feedback.deliver(self.store, event))
                self.assertTrue(antigravity_feedback.deliver(self.store, event))
                self.assertEqual(len(self.store.list()), 1 if index == 0 else 2)
            other = transcript.parent / "other.jsonl"
            other.write_bytes(transcript.read_bytes())
            self.assertFalse(antigravity_feedback.deliver(self.store, {**event, "transcriptPath": str(other)}))
        self.assertEqual(len(self.store.list()), 2)
        self.assertNotIn(b"private", self.store.path.read_bytes())

    def test_question_hook_tracks_each_request_and_leaves_permissions_unchanged(self):
        config = self.root / "device.json"
        command = [sys.executable, "-m", "deskpet_platform", "--config", str(config), "antigravity-hook"]

        def receive(phase, kind, request, *, tool="ask_question"):
            event = {"hookEventName": kind, "conversationId": "conversation", "workspacePaths": ["/sample/project"],
                     "stepIdx": 2, "toolCall": {"name": tool, "id": request, "args": {
                         "questions": [{"question": "private question", "options": ["First", "Second"]}]}}}
            result = subprocess.run([*command, "--phase", phase], input=json.dumps(event),
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        self.assertEqual(receive("before", "PreToolUse", "ignored", tool="run_command"), {"decision": "allow"})
        self.assertEqual(receive("before", "PreToolUse", "one"), {"decision": "allow"})
        self.assertEqual(receive("before", "PreToolUse", "two"), {"decision": "allow"})
        self.assertEqual(receive("before", "PreToolUse", "one"), {"decision": "allow"})
        self.assertEqual(receive("after", "PostToolUse", "one"), {})
        self.assertEqual([(r["source"], r["category"], r["resolved"]) for r in self.store.list()],
                         [("antigravity", "response", False), ("antigravity", "response", True)])
        self.assertNotIn(b"private question", self.store.path.read_bytes())

    def test_upgrade_replaces_only_the_previously_installed_stop_hook(self):
        path = self.root / "hooks.json"
        command = codex_hook.hook_command(self.root / "device.json", "antigravity-hook")
        name = "deskpet-" + hashlib.sha256(command.encode()).hexdigest()[:16]
        previous = {"Stop": [{"type": "command", "command": command, "timeout": 2}]}
        path.write_text(json.dumps({name: previous, "other": previous}))
        self.assertTrue(antigravity_feedback.configure(path, command, enabled=True))
        hooks = json.loads(path.read_text())
        self.assertEqual(set(hooks[name]), {"Stop", "PreToolUse", "PostToolUse"})
        self.assertEqual(hooks["other"], previous)
        self.assertTrue(antigravity_feedback.configure(path, command, enabled=False))
        self.assertEqual(json.loads(path.read_text()), {"other": previous})
