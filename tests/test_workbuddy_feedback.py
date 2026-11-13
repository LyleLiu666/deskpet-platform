import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import workbuddy_feedback
from deskpet_platform.notifications import NotificationStore


class WorkBuddyFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        home = patch("deskpet_platform.workbuddy_feedback.Path.home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.store = NotificationStore(self.root / "notifications.sqlite3")

    def test_completed_turns_are_deduplicated_without_storing_transcript_text(self):
        transcript = self.root / ".workbuddy-ai/projects/example/session.jsonl"
        transcript.parent.mkdir(parents=True)
        event = {"hook_event_name": "Stop", "session_id": "session", "cwd": "/tmp/example",
                 "transcript_path": str(transcript), "stop_hook_active": False}

        def append(role, timestamp=None, **fields):
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "message", "role": role, "timestamp": timestamp,
                                         "content": "private transcript", **fields}) + "\n")

        def stop(**fields):
            return workbuddy_feedback.deliver(self.store, {**event, **fields}, edition="workbuddy_ai")

        append("user", 100)
        append("assistant", 101)
        self.assertTrue(stop())
        self.assertTrue(stop())
        append("user", 102)
        append("assistant", 103, providerData={"isMeta": True})
        with patch("deskpet_platform.workbuddy_feedback.time.sleep"):
            self.assertFalse(stop())
        append("assistant", 104)
        self.assertFalse(stop(is_truncation_recovery=True))
        self.assertTrue(stop())
        append("user")
        append("assistant")
        self.assertTrue(stop())
        self.assertTrue(stop())
        with patch("deskpet_platform.workbuddy_feedback.time.sleep"):
            self.assertFalse(workbuddy_feedback.deliver(self.store, event))
        self.assertEqual([(item["source"], item["title"]) for item in self.store.list()],
                         [("workbuddy", "example")] * 3)
        self.assertNotIn(b"private transcript", self.store.path.read_bytes())

    def test_question_results_clear_only_the_matching_pending_event(self):
        event = {"hook_event_name": "PreToolUse", "session_id": "session", "tool_name": "AskUserQuestion",
                 "call_id": "call-1", "tool_input": {"questions": [{"question": "private prompt"}]}}
        self.assertTrue(workbuddy_feedback.deliver(self.store, event))
        self.assertTrue(workbuddy_feedback.deliver(self.store, {**event, "call_id": "call-2"}))
        self.assertFalse(workbuddy_feedback.deliver(self.store, {**event, "tool_name": "Read"}))
        self.assertFalse(workbuddy_feedback.deliver(self.store, {**event, "hook_event_name": "PostToolUse",
                                                                 "session_id": "other"}))
        self.assertTrue(workbuddy_feedback.deliver(self.store, {**event, "hook_event_name": "PostToolUse"}))
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertTrue(workbuddy_feedback.deliver(self.store, {**event, "call_id": "call-2",
                                                                "hook_event_name": "PostToolUseFailure"}))
        self.assertEqual(self.store.home_summary()["response_count"], 0)
        self.assertNotIn(b"private prompt", self.store.path.read_bytes())

    def test_sdk_pre_tool_with_complete_answers_does_not_open_a_pending_request(self):
        questions = [{"question": "First private prompt"}, {"question": "Second private prompt"}]
        event = {"hook_event_name": "PreToolUse", "session_id": "session", "tool_name": "AskUserQuestion",
                 "tool_use_id": "call-1", "tool_input": {"questions": questions}}
        answered = {**event, "tool_input": {"questions": questions,
                    "answers": {"First private prompt": "One", "Second private prompt": "Two"}}}
        partial = {**event, "tool_use_id": "call-2", "tool_input": {"questions": questions,
                   "answers": {"First private prompt": "One", "Second private prompt": " "}}}
        self.assertFalse(workbuddy_feedback.deliver(self.store, answered))
        self.assertEqual(self.store.list(), [])
        self.assertTrue(workbuddy_feedback.deliver(self.store, partial))
        self.assertTrue(workbuddy_feedback.deliver(self.store, event))
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertEqual(sum(row["resolved"] for row in self.store.list()), 1)

        self.assertTrue(workbuddy_feedback.deliver(self.store, {**answered, "tool_use_id": "call-2"}))
        self.assertEqual(self.store.home_summary()["response_count"], 0)
        self.assertTrue(all(row["resolved"] for row in self.store.list()))
        self.assertFalse(workbuddy_feedback.deliver(self.store, {**answered, "hook_event_name": "PostToolUse"}))
        self.assertNotIn(b"private prompt", self.store.path.read_bytes())

    def test_permission_reminder_uses_the_explicit_generation_and_stop(self):
        event = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                 "session_id": "session-a", "generation_id": "generation", "message": "private prompt"}
        self.assertTrue(workbuddy_feedback.deliver(self.store, event))
        self.assertTrue(workbuddy_feedback.deliver(self.store, event))
        self.assertTrue(workbuddy_feedback.deliver(self.store, {**event, "session_id": "session-b"}))
        self.assertFalse(workbuddy_feedback.deliver(self.store, {**event, "notification_type": "idle_prompt"}))
        self.assertFalse(workbuddy_feedback.deliver(self.store, {**event, "generation_id": None}))
        self.assertEqual(self.store.home_summary()["response_count"], 2)
        stop = {**event, "hook_event_name": "Stop", "stop_hook_active": False}
        self.assertTrue(workbuddy_feedback.deliver(self.store, stop))
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertTrue(workbuddy_feedback.deliver(self.store, event))
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertNotIn(b"private prompt", self.store.path.read_bytes())
