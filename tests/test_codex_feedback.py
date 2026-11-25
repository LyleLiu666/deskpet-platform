from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
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

    def test_concurrent_settings_change_is_not_overwritten(self):
        path = Path(self.temp.name) / "settings.json"
        path.write_text('{"user": "old"}', encoding="utf-8")
        original = path.read_bytes()
        path.write_text('{"user": "new"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "其他程序修改"):
            codex_hook._write(path, {"hooks": {}}, original=original)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"user": "new"})
        with self.assertRaisesRegex(ValueError, "其他程序修改"):
            codex_hook._write(path, {"hooks": {}}, original=None)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"user": "new"})

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
        codex_feedback.deliver(self.store, self.answer([0]))
        self.assertEqual(self.store.home_summary()["completed_count"], 1)
        codex_feedback.deliver(self.store, self.answer([0, 1]))
        self.assertEqual(len([item for item in self.store.list() if item["category"] == "completed"]), 2)
        self.assertIsNotNone(self.store.add_codex_event(stop))

    def test_failed_question_releases_a_stop_received_earlier(self):
        codex_feedback.deliver(self.store, self.event)
        stop = {"hook_event_name": "Stop", "session_id": "session-1", "turn_id": "turn-1"}
        self.assertIsNone(self.store.add_codex_event(stop))
        codex_feedback.deliver(self.store, {**self.event, "hook_event_name": "PostToolUse",
                                          "tool_response": "failed to parse function arguments: duplicate field"})
        self.assertEqual(self.store.home_summary()["response_count"], 0)
        self.assertEqual(self.store.home_summary()["completed_count"], 1)

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

    def test_reconcile_native_failures_answers_aborts_and_archives(self):
        home = Path(self.temp.name)
        live = "01020304-0000-4000-8000-000000000001"
        archived = "01020304-0000-4000-8000-000000000002"
        rollout = home / "sessions" / "live.jsonl"
        rollout.parent.mkdir()
        with closing(sqlite3.connect(home / "state_5.sqlite")) as state, state:
            state.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
            state.executemany("INSERT INTO threads VALUES (?,?,?)", ((live, str(rollout), 0), (archived, "", 1)))
        for call, turn in (("failed", "one"), ("call_123", "two"), ("aborted", "three"), ("still-live", "four")):
            codex_feedback.deliver(self.store, {**self.event, "session_id": live, "tool_use_id": call, "turn_id": turn})
        codex_feedback.deliver(self.store, {**self.event, "session_id": archived})
        for session, turn in ((live, "one"), (live, "three"), (archived, "turn-1")):
            self.assertIsNone(self.store.add_codex_event({
                "hook_event_name": "Stop", "session_id": session, "turn_id": turn,
            }))
        events = [
            ("response_item", {"type": "function_call_output", "call_id": "failed",
                               "output": "failed to parse function arguments: duplicate field"}),
            ("response_item", {"type": "function_call_output", "call_id": "still-live", "output": '{"accepted":true}'}),
            ("event_msg", {"type": "task_complete", "turn_id": "four"}),
            ("event_msg", {"type": "turn_aborted", "turn_id": "three"}),
            ("event_msg", {"type": "user_message", "message": self.answer([0])["prompt"]}),
        ]
        rollout.write_text("".join(json.dumps({"type": kind, "payload": payload}) + "\n" for kind, payload in events))
        reconcile = codex_feedback.QuestionReconciler(self.store, home)
        self.assertEqual(reconcile.run(), 7)
        self.assertEqual(self.store.home_summary()["response_count"], 3)
        self.assertEqual(len([item for item in self.store.list() if item["category"] == "completed"]), 1)
        self.assertEqual(reconcile.run(), 0)
        with rollout.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": self.answer([1])["prompt"],
            }}) + "\n")
        self.assertEqual(reconcile.run(), 1)
        self.assertEqual(self.store.home_summary()["response_count"], 2)

    def rollout(self):
        home = Path(self.temp.name)
        path = home / "sessions" / "desktop.jsonl"
        path.parent.mkdir()
        path.touch()
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id, rollout_path, archived)")
            db.execute("INSERT INTO threads VALUES(?,?,0)", (self.event["session_id"], str(path)))

        def append(payload):
            with path.open("a") as stream:
                print(json.dumps({"type": "response_item", "payload": payload}), file=stream)

        return codex_feedback.QuestionReconciler(self.store, home), append

    def test_desktop_user_input_blocks_reconcile_only_matching_replies(self):
        reconcile, append = self.rollout()
        codex_feedback.deliver(self.store, self.event)
        block = {"type": "input_text", "text": self.answer([1])["prompt"]}
        append({"type": "message", "role": "assistant", "content": [block]})
        append({"type": "message", "role": "user", "content": [{**block, "type": "output_text"}]})
        self.assertEqual(reconcile.run(), 0)
        append({"type": "message", "role": "user", "content": [{"type": "input_image"}, block, block]})
        self.assertEqual(reconcile.run(), 1)
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertEqual(reconcile.run(), 0)

    def test_cli_rollout_pairs_matching_call_across_poll_and_partial_write(self):
        reconcile, append = self.rollout()
        event = {**self.event, "tool_name": codex_feedback.CLI_TOOL,
                 "tool_input": {"questions": [{"id": "first"}, {"id": "second"}]}}
        codex_feedback.deliver(self.store, event)
        codex_feedback.deliver(self.store, {**self.event, "turn_id": "another-turn", "tool_use_id": "keep-open"})
        output = {"type": "function_call_output", "call_id": event["tool_use_id"],
                  "output": json.dumps({"answers": {"second": {"answers": ["private answer"]}}})}
        append({**output, "call_id": "unrelated"})
        append(output)
        self.assertEqual(reconcile.run(), 0)
        append({"type": "function_call", "name": "request_user_input", "call_id": event["tool_use_id"],
                "arguments": json.dumps(event["tool_input"])})
        self.assertEqual(reconcile.run(), 0)
        append({**output, "output": '{"answers":{"other":{"answers":[]}}}'})
        append({**output, "call_id": "another-call"})
        self.assertEqual(reconcile.run(), 0)
        line = json.dumps({"type": "response_item", "payload": output})
        path = Path(self.temp.name) / "sessions/desktop.jsonl"
        with path.open("a") as stream:
            stream.write(line[:20])
        self.assertEqual(reconcile.run(), 0)
        with path.open("a") as stream:
            print(line[20:], file=stream)
        self.assertEqual(reconcile.run(), 2)
        remaining = {identity for _, identity, _ in self.store.open_codex_questions()}
        self.assertEqual(remaining, {codex_feedback.question_id("keep-open", index) for index in (0, 1)})

    def test_locked_inbox_recovers_last_answer_without_another_hook(self):
        config = Path(self.temp.name) / "device.json"
        codex_feedback.deliver(self.store, self.event)
        with closing(sqlite3.connect(self.store.path)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            with patch("deskpet_platform.codex_hook.subprocess.Popen") as spawn, \
                    self.assertLogs(codex_feedback.__name__, level="WARNING") as logged:
                codex_hook.receive_event(config, self.answer([1]))
            self.assertNotIn("private answer", " ".join(logged.output))
            self.assertLess(time.monotonic() - start, 1.5)
            spawn.assert_called_once()
            receipts = list(self.store.path.parent.glob("codex-replies/*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertNotIn("private answer", receipts[0].read_text())
            # Start the same detached command while the writer still holds the lock.
            with subprocess.Popen(*spawn.call_args.args, **spawn.call_args.kwargs) as worker:
                try:
                    locker.rollback()
                    worker.wait(timeout=10)
                finally:
                    if worker.poll() is None:
                        worker.kill()
            self.assertEqual(worker.returncode, 0)
        self.assertEqual(self.store.home_summary()["response_count"], 1)
        self.assertFalse(receipts[0].exists())
        self.assertEqual(codex_feedback.deliver(self.store, self.answer([1])), 0)

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
