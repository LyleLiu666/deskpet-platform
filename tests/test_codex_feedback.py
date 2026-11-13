import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import codex_feedback, codex_hook
from deskpet_platform.notifications import NotificationStore


class CodexFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = NotificationStore(Path(self.temp.name) / "notifications.sqlite3")
        self.event = {"hook_event_name": "PreToolUse", "session_id": "session-1", "turn_id": "turn-1",
                      "cwd": "/tmp/project", "tool_name": "request_user_input_async", "tool_use_id": "call_123",
                      "tool_input": {"questions": [{"title": "private question 1"}, {"title": "private question 2"}]}}

    @staticmethod
    def answer(indices, *, session="session-1"):
        items = [{"questionItemId": json.dumps(["request_user_input_async", "call_123", index]),
                  "answer": "private answer"} for index in indices]
        return {"hook_event_name": "UserPromptSubmit", "session_id": session,
                "prompt": "<send_user_message_question_reply>\n" + json.dumps(items) + "\n</send_user_message_question_reply>"}

    def test_questions_and_responses_are_exact_and_private(self):
        with patch("deskpet_platform.notifications.CODEX_STATE", Path(self.temp.name) / "missing.sqlite"):
            self.assertEqual(codex_feedback.deliver(self.store, self.event), 2)
            codex_feedback.deliver(self.store, self.event)
        self.assertEqual(self.store.home_summary()["response_sources"][0]["count"], 2)
        self.assertNotIn(b"private question", self.store.path.read_bytes())
        self.assertEqual(codex_feedback.deliver(self.store, self.answer([0])), 1)
        self.assertEqual(self.store.home_summary()["response_sources"][0]["count"], 1)
        self.assertEqual(codex_feedback.deliver(self.store, self.answer([0, 1])), 1)
        self.assertEqual(self.store.home_summary()["response_source_count"], 0)
        self.assertNotIn(b"private answer", self.store.path.read_bytes())

    def test_completion_waits_for_questions_from_the_same_turn(self):
        codex_feedback.deliver(self.store, self.event)
        stop = {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1"}
        self.assertIsNone(self.store.add_codex_event(stop))
        self.assertIsNotNone(self.store.add_codex_event({**stop, "turn_id": "other-turn"}))
        codex_feedback.deliver(self.store, self.answer([0, 1]))
        self.assertIsNotNone(self.store.add_codex_event(stop))

    def test_unrelated_content_and_sessions_cannot_resolve_questions(self):
        codex_feedback.deliver(self.store, self.event)
        self.assertEqual(codex_feedback.deliver(self.store, self.answer([0, 1], session="another")), 0)
        for prompt in ("普通聊天问题？", "<send_user_message_question_reply>bad</send_user_message_question_reply>"):
            self.assertEqual(codex_feedback.deliver(self.store, {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "prompt": prompt}), 0)
        self.assertEqual(self.store.home_summary()["response_sources"][0]["count"], 2)
        self.assertEqual(codex_feedback.deliver(self.store, {**self.event, "hook_event_name": "PostToolUse", "tool_response": {"accepted": True}}), 0)
        self.assertEqual(codex_feedback.deliver(self.store, {**self.event, "hook_event_name": "PostToolUse", "tool_response": {"accepted": False}}), 2)
        self.assertEqual(self.store.home_summary()["response_source_count"], 0)

    def test_answer_arriving_before_question_does_not_leave_a_false_pending_notice(self):
        self.assertEqual(codex_feedback.deliver(self.store, self.answer([0])), 0)
        codex_feedback.deliver(self.store, self.event)
        self.assertEqual(self.store.home_summary()["response_sources"][0]["count"], 1)

    def test_configured_hooks_receive_questions_and_clear_them_on_answer(self):
        hooks_path = self.store.path.parent / "hooks.json"
        config_path = self.store.path.parent / "device.json"
        command = codex_hook.hook_command(config_path)
        self.assertTrue(codex_hook.install_hook(hooks_path, command))
        hooks = json.loads(hooks_path.read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], codex_feedback.TOOL)
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], codex_feedback.TOOL)
        self.assertIn("UserPromptSubmit", hooks)
        with patch("deskpet_platform.codex_hook.subprocess.Popen") as spawn:
            for event, pending in ((self.event, 2), (self.answer([0, 1]), 0)):
                with patch("sys.stdin", io.StringIO(json.dumps(event))), patch("sys.stdout", io.StringIO()):
                    codex_hook.receive(config_path)
                self.assertEqual(self.store.home_summary()["response_count"], pending)
        self.assertEqual(spawn.call_count, 2)


if __name__ == "__main__":
    unittest.main()
