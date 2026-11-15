import io
import json
import os
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

    def test_question_carries_project_and_session_title_without_question_text(self):
        with patch("deskpet_platform.codex_feedback.session_title", return_value="Session title"):
            codex_feedback.deliver(self.store, self.event)
        record = self.store.cache_records(active_only=True)[0]
        self.assertIn("project", record["title"])
        self.assertIn("Session title", record["title"])
        self.assertNotIn("private question", record["title"])

    def test_questions_and_responses_are_exact_and_private(self):
        with patch.dict(os.environ, {"CODEX_HOME": self.temp.name}):
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

    def test_answered_retry_does_not_restore_the_earlier_call(self):
        initial = {**self.event, "tool_use_id": "failed-call"}
        codex_feedback.deliver(self.store, initial)
        codex_feedback.deliver(self.store, self.event)
        codex_feedback.deliver(self.store, self.answer([1]))
        mirror = NotificationStore(self.store.path.with_name("mirror.sqlite3"), source=self.store)
        mirror.sync()
        records = self.store.cache_records(active_only=True)
        self.assertEqual([row["id"] for row in records], [3])
        self.assertEqual(mirror.cache_records(active_only=True), records)
        self.assertEqual(self.store.seen_notifications([1, 2, 3, 4], include_resolved=True), {1, 2, 4})
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        codex_feedback.deliver(self.store, self.answer([0]))
        self.assertEqual(self.store.cache_records(active_only=True), [])
        mirror.sync()
        self.assertEqual(mirror.cache_records(active_only=True), [])
        self.assertEqual(self.store.home_summary()["response_count"], 0)
        stop = {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1"}
        self.assertIsNotNone(self.store.add_codex_event(stop))

    def test_question_batch_rolls_back_when_one_identity_conflicts(self):
        conflicting = codex_feedback.question_id(self.event["tool_use_id"], 1)
        self.store.add_agent_event("codex", "session-1", conflicting, "completed", "Existing")
        with self.assertRaisesRegex(ValueError, "conflicting_agent_event"):
            codex_feedback.deliver(self.store, self.event)
        self.assertEqual(self.store.home_summary()["response_count"], 0)

    def test_cli_reply_closes_exact_questions_and_keeps_payload_private(self):
        questions = [{"id": "first", "title": "private opening"}, {"id": "second"}]
        event = {**self.event, "tool_name": codex_feedback.CLI_TOOL, "tool_input": {"questions": questions}}
        post = {**event, "hook_event_name": "PostToolUse"}
        payload = {"answers": {"second": {"answers": ["private answer"]}}}
        self.assertEqual(codex_feedback.cli_question_outcomes(questions, json.dumps(payload)),
                         ("cancelled", "answered"))
        self.assertEqual(codex_feedback.cli_question_outcomes(questions, {"answers": {}}),
                         ("cancelled", "cancelled"))
        self.assertEqual(codex_feedback.cli_question_outcomes(questions, {**payload, "accepted": False}),
                         ("cancelled", "cancelled"))
        self.assertEqual(codex_feedback.deliver(self.store, event), 2)
        self.assertEqual(codex_feedback.deliver(self.store, {**post, "tool_response": "broken"}), 0)
        self.assertEqual(codex_feedback.deliver(self.store, {
            **post, "tool_response": {"answers": {"unrelated": {"answers": ["private answer"]}}},
        }), 0)
        self.assertEqual(self.store.home_summary()["response_count"], 2)
        self.assertEqual(codex_feedback.deliver(self.store, {**post, "tool_response": payload}), 2)
        self.assertEqual(codex_feedback.deliver(self.store, {**post, "tool_response": payload}), 0)
        self.assertEqual(self.store.home_summary()["response_count"], 0)
        self.assertNotIn(b"private", self.store.path.read_bytes())

    def test_install_appends_cli_groups_without_rewriting_existing_async_hooks(self):
        path = Path(self.temp.name) / "hooks.json"
        command = codex_hook.hook_command(path.with_name("device.json"))
        codex_hook.install_hook(path, command, matchers=dict(codex_hook.EVENT_MATCHERS))
        previous = json.loads(path.read_text())
        unrelated = {"matcher": "*", "hooks": [{"type": "command", "command": "unrelated"}]}
        previous["hooks"]["PostToolUse"].append(unrelated)
        path.write_text(json.dumps(previous))
        self.assertTrue(codex_hook.install_hook(path, command))
        self.assertFalse(codex_hook.install_hook(path, command))
        upgraded = json.loads(path.read_text())
        for event, matcher in codex_hook.CLI_EVENT_MATCHERS.items():
            groups = upgraded["hooks"][event]
            self.assertEqual(groups[:-1], previous["hooks"][event])
            self.assertEqual(groups[-1], {"matcher": matcher, "hooks": [
                {"type": "command", "command": command, "timeout": 2},
            ]})
        self.assertTrue(codex_hook.remove_hook(path, command))
        self.assertFalse(codex_hook.remove_hook(path, command))
        self.assertEqual(json.loads(path.read_text())["hooks"], {
            "PostToolUse": [{**previous["hooks"]["PostToolUse"][0], "hooks": []}, unrelated],
        })

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
