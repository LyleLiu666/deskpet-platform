import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import claude_feedback, codex_hook
from deskpet_platform.notifications import NotificationStore


class ClaudeFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        env.start()
        self.addCleanup(env.stop)
        self.store = NotificationStore(self.root / "notifications.sqlite3")

    def test_old_stop_event_uses_the_same_sessions_assistant_identity(self):
        transcript = self.root / "claude/projects/project/session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(json.dumps(record) for record in (
            {"type": "user", "sessionId": "session", "message": {"content": "private prompt"}},
            {"type": "assistant", "sessionId": "session", "uuid": "reply", "message": {"content": "private reply"}},
            {"type": "assistant", "sessionId": "other", "uuid": "irrelevant"},
        )))
        event = {"hook_event_name": "Stop", "session_id": "session", "cwd": "/tmp/fixture",
                 "transcript_path": str(transcript), "stop_hook_active": False}
        self.assertTrue(claude_feedback.deliver(self.store, event))
        self.assertTrue(claude_feedback.deliver(self.store, event))
        self.assertFalse(claude_feedback.deliver(self.store, {**event, "stop_hook_active": True}))
        self.assertEqual([(item["source"], item["title"]) for item in self.store.list()], [("claude_code", "fixture")])
        self.assertNotIn(b"private", self.store.path.read_bytes())

    def test_only_the_matching_question_is_cleared_by_the_result(self):
        event = {"hook_event_name": "PreToolUse", "session_id": "session", "tool_name": "AskUserQuestion",
                 "tool_use_id": "question-1", "tool_input": {"questions": [{"question": "private prompt"}]}}
        self.assertTrue(claude_feedback.deliver(self.store, event))
        self.assertTrue(claude_feedback.deliver(self.store, {**event, "tool_use_id": "question-2"}))
        self.assertFalse(claude_feedback.deliver(self.store, {**event, "tool_name": "Read"}))
        self.assertFalse(claude_feedback.deliver(self.store, {**event, "hook_event_name": "PostToolUse",
                                                             "session_id": "other"}))
        self.assertTrue(claude_feedback.deliver(self.store, {**event, "hook_event_name": "PostToolUse"}))
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertTrue(claude_feedback.deliver(self.store, {**event, "tool_use_id": "question-2",
                                                            "hook_event_name": "PostToolUseFailure"}))
        self.assertEqual(self.store.home_summary()["response_count"], 0)

    def test_native_dismissal_settles_only_an_existing_request_in_that_session(self):
        event = {"hook_event_name": "PreToolUse", "session_id": "session", "tool_name": "AskUserQuestion",
                 "tool_use_id": "cancelled", "tool_input": {"questions": [{"question": "fixture"}]}}
        for request in (event, {**event, "tool_use_id": "still-pending"}, {**event, "session_id": "other"}):
            self.assertTrue(claude_feedback.deliver(self.store, request))
        transcript = self.root / "claude/projects/project/session.jsonl"
        transcript.parent.mkdir(parents=True)

        def rejection(tool_use_id, **fields):
            return {"type": "user", "sessionId": "session", "toolDenialKind": "user-rejected",
                    "message": {"content": [{"type": "tool_result", "is_error": True,
                                             "tool_use_id": tool_use_id}]}, **fields}

        transcript.write_text("\n".join(json.dumps(entry) for entry in (
            rejection("cancelled"), rejection("still-pending", isSidechain=True), rejection("unseen"),
        )))
        followup = {"hook_event_name": "UserPromptSubmit", "session_id": "session",
                    "transcript_path": str(transcript)}
        self.assertTrue(claude_feedback.deliver(self.store, followup))
        self.assertFalse(claude_feedback.deliver(self.store, followup))
        self.assertEqual(self.store.home_summary()["response_count"], 2)
        self.assertTrue(claude_feedback.deliver(self.store, {**event, "tool_use_id": "unseen"}))
        self.assertEqual(self.store.home_summary()["response_count"], 3)

        stop = {**followup, "hook_event_name": "Stop", "prompt_id": "next-turn"}
        self.assertTrue(claude_feedback.deliver(self.store, stop))
        self.assertEqual(self.store.home_summary()["response_count"], 2)
        self.assertEqual(sum(row["category"] == "completed" for row in self.store.list()), 1)

    def test_installer_preserves_unrelated_claude_settings(self):
        path = claude_feedback.settings_path()
        path.parent.mkdir(parents=True)
        existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]},
                    "permissions": {"defaultMode": "default"}}
        path.write_text(json.dumps(existing))
        path.chmod(0o644)
        command = codex_hook.hook_command(self.root / "device.json", "claude-hook")
        options = {"matchers": claude_feedback.MATCHERS, "private": False}
        self.assertTrue(codex_hook.install_hook(path, command, **options))
        self.assertFalse(codex_hook.install_hook(path, command, **options))
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        installed = json.loads(path.read_text())
        self.assertEqual(installed["hooks"]["PreToolUse"][0]["matcher"], "AskUserQuestion")
        self.assertEqual(installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], command)
        self.assertEqual(installed["hooks"]["Stop"][0], existing["hooks"]["Stop"][0])
        self.assertTrue(codex_hook.remove_hook(path, command, **options))
        self.assertEqual(json.loads(path.read_text()), existing)
