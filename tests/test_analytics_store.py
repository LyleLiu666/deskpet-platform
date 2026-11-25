from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from deskpet_platform.analytics_store import AnalyticsStore

NOW = datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
ZONE = timezone(timedelta(hours=8))


def codex_usage(stamp, amount, response, total):
    return {"type": "token_usage_record", "timestamp": stamp, "payload":
            {"response_id": response, "usage": {"output_tokens": amount},
             "thread_token_usage": {"output_tokens": total}}}


def codex_legacy(stamp, amount, total):
    return {"type": "event_msg", "timestamp": stamp, "payload":
            {"type": "token_count", "info": {"last_token_usage": {"output_tokens": amount},
             "total_token_usage": {"output_tokens": total}}}}


class AnalyticsStoreTest(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.home = Path(root.name)
        self.store = AnalyticsStore(self.home / "config/analytics.sqlite3", home=self.home, timezone=ZONE)
        self.now = NOW.timestamp()

    def write(self, name, *rows, append=False, partial=False):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab" if append else "wb") as stream:
            for row in rows:
                stream.write(json.dumps(row).encode())
                if not partial:
                    stream.write(b"\n")
        return path

    def read(self, source):
        self.store.collect(self.now)
        return self.store.compute_source_payload(source, self.now)

    def test_midnight_deduplication_incomplete_line_and_replacement(self):
        name = ".codex/sessions/2026/11/20/rollout.jsonl"
        meta = {"type": "session_meta", "payload": {"id": "session"}}
        start = "2026-11-24T15:59:59Z"
        next_day = "2026-11-24T16:00:00Z"
        one, two = codex_usage(start, 7, "one", 7), codex_usage(next_day, 5, "two", 12)
        path = self.write(name, meta, one, one, codex_legacy(next_day, 7, 7))
        self.write(name, two, append=True, partial=True)
        self.assertEqual(self.read(0)["days"], [(20261124, 7, 1)])
        with path.open("ab") as stream:
            stream.write(b"\n")
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 5, 1), (20261124, 7, 1)])
        self.assertEqual(result["sessions"][0]["output_tokens"], 12)
        self.assertEqual(self.read(0), result)
        replacement = self.write(".replacement", meta, two)
        replacement.replace(path)
        self.assertEqual(self.read(0)["days"], [(20261125, 5, 1)])
        self.write(name, meta, one)
        self.assertEqual(self.read(0)["days"], [(20261124, 7, 1)])

    def test_late_modern_record_replaces_legacy_without_double_counting(self):
        name = ".codex/sessions/2026/11/25/rollout.jsonl"
        stamp = "2026-11-25T05:00:00Z"
        self.write(name, codex_legacy(stamp, 9, 9))
        self.assertEqual(self.read(0)["days"], [(20261125, 9, 1)])
        self.write(name, codex_usage(stamp, 9, "first", 9), append=True)
        self.assertEqual(self.read(0)["days"], [(20261125, 9, 1)])

    def test_real_schema_projections_keep_measured_zero_and_known_provider(self):
        stamp = "2026-11-25T08:00:00Z"
        for amount in (0, 3):
            self.write(".claude/projects/p/s.jsonl", {"type": "assistant", "timestamp": stamp, "sessionId": "s",
                       "message": {"id": "message", "usage": {"output_tokens": amount}}}, append=True)
        self.write(".claude/projects/p/zero.jsonl", {"type": "assistant", "timestamp": "2026-11-23T08:00:00Z",
                   "message": {"id": "zero", "usage": {"output_tokens": 0}}})
        self.assertEqual(self.read(1)["days"], [(20261125, 3, 1), (20261123, 0, 1)])
        ag = {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": stamp}
        self.write(".gemini/antigravity/brain/task/.system_generated/logs/transcript.jsonl",
                   dict(ag, type="USER_INPUT", source="USER_EXPLICIT", step_index=0),
                   dict(ag, model="gemini-4-argon-high", output_tokens=11, step_index=1),
                   dict(ag, model="claude-opus-5-5-high", output_tokens=17, step_index=2),
                   dict(ag, model=None, output_tokens=23, step_index=3))
        gemini = self.read(2)
        self.assertEqual(gemini["days"], [(20261125, 11, 1)])
        self.assertEqual(gemini["sessions"][0]["turns"], 1)
        self.assertEqual(self.read(3)["days"], [(20261125, 17, 1)])
        wb = {"type": "message", "role": "assistant", "timestamp": int(self.now * 1000), "sessionId": "wb",
              "providerData": {"messageId": "response"}, "message": {"usage": {"output_tokens": 19}}}
        self.write(".workbuddy/projects/p/wb.jsonl", wb, dict(wb, type="function_call"))
        self.assertEqual(self.read(5)["days"], [(20261125, 19, 1)])

    def test_opencode_reads_database_and_picks_up_updates(self):
        path = self.home / ".local/share/opencode/opencode.db"
        path.parent.mkdir(parents=True)
        stamp = int(self.now * 1000)
        with closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE session(id,title,parent_id,time_updated);"
                             "CREATE TABLE message(id,session_id,time_created,time_updated,data);")
            db.execute("INSERT INTO session VALUES ('test','会话标题',NULL,?)", (stamp,))
            db.execute("INSERT INTO message VALUES ('m','test',?,?,?)",
                       (stamp, stamp, json.dumps({"role": "assistant", "tokens": {"output": 12}})))
            db.commit()
            self.assertEqual(self.read(4)["days"], [(20261125, 12, 1)])
            db.execute("UPDATE message SET data=?", (json.dumps({"role": "assistant", "tokens": {"output": 14}}),))
            db.commit()
            self.assertEqual(self.read(4)["sessions"][0]["output_tokens"], 14)

    def test_only_quota_change_keeps_rollup_fingerprint(self):
        before = self.read(0)
        self.store.record_quota(0, self.now, {"quotas": [[4, 10080, None]], "credits": "12.00"})
        after = self.read(0)
        self.assertEqual(before["rollup_crc"], after["rollup_crc"])
        self.assertNotEqual(before["quota_crc"], after["quota_crc"])
        self.assertTrue(after["quota"]["low_warning"])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_statusline_snapshot_keeps_original_time_after_window_expires(self):
        from deskpet_platform.quota_capture import QuotaStore

        quota_store = QuotaStore(self.home / "config/quota.sqlite3")
        past = int(self.now) - 7200
        quota_store.record("claude_code", {"rate_limits": {
            "five_hour": {"used_percentage": 11, "resets_at": past + 3600},
        }}, past)
        self.store.collect_quota(quota_store)
        projected = self.store.compute_source_payload(1, self.now)["quota"]
        self.assertEqual(projected["observed_at"], int(past))
        self.assertEqual(projected["windows"], [[89, 300, int(past + 3600)]])
