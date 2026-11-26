from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deskpet_platform.analytics_collectors import jsonl_events
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


def _varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append(value & 127 | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _protobuf_blob(field, value):
    return _varint(field << 3 | 2) + _varint(len(value)) + value


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
        start_first = {"type": "event_msg", "timestamp": start,
                       "payload": {"type": "task_started", "turn_id": "turn-one"}}
        start_second = {"type": "event_msg", "timestamp": next_day,
                        "payload": {"type": "task_started", "turn_id": "turn-two"}}
        path = self.write(name, meta, start_first, one, one, codex_legacy(next_day, 7, 7), start_second)
        self.write(name, two, append=True, partial=True)
        self.assertEqual(self.read(0)["days"], [(20261124, 7, 1)])
        with path.open("ab") as stream:
            stream.write(b"\n")
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 5, 1), (20261124, 7, 1)])
        self.assertEqual(result["sessions"][0]["output_tokens"], 12)
        self.assertEqual(result["sessions"][0]["turns"], 2)
        self.assertEqual(self.read(0), result)
        replacement = self.write(".replacement", meta, two)
        replacement.replace(path)
        self.assertEqual(self.read(0)["days"], [(20261125, 5, 1)])
        self.write(name, meta, one)
        self.assertEqual(self.read(0)["days"], [(20261124, 7, 1)])

    def test_large_log_append_starts_at_the_saved_offset(self):
        name = ".codex/sessions/2026/11/25/large.jsonl"
        stamp = "2026-11-25T05:00:00Z"
        meta = {"type": "session_meta", "payload": {"id": "stable-session"}}
        sample = codex_usage(stamp, 7, "first", 7)
        sample["padding"] = "fixture" * 120
        path = self.write(name, meta, *[sample] * 2500)
        previous = path.stat().st_size
        self.assertGreater(previous, 2 * 1024 * 1024)
        before = self.read(0)

        self.write(name, codex_usage(stamp, 11, "second", 18), append=True)
        offsets = []

        def observe(kind, stream, session, **options):
            start = stream.tell()
            yield from jsonl_events(kind, stream, session, **options)
            offsets.append((start, stream.tell()))

        with patch("deskpet_platform.analytics_store.jsonl_events", side_effect=observe):
            after = self.read(0)
        self.assertEqual(offsets, [(previous, path.stat().st_size)])
        self.assertEqual(before["days"], [(20261125, 7, 1)])
        self.assertEqual(after["days"], [(20261125, 18, 1)])
        self.assertEqual(after["sessions"][0]["key"], before["sessions"][0]["key"])
        self.assertEqual(after["sessions"][0]["output_tokens"], 18)

    def test_late_modern_record_replaces_legacy_without_double_counting(self):
        name = ".codex/sessions/2026/11/25/rollout.jsonl"
        stamp = "2026-11-25T05:00:00Z"
        self.write(name, codex_legacy(stamp, 9, 9))
        self.assertEqual(self.read(0)["days"], [(20261125, 9, 1)])
        self.write(name, codex_usage(stamp, 9, "first", 9), append=True)
        self.assertEqual(self.read(0)["days"], [(20261125, 9, 1)])

    def test_codex_mirror_matches_cumulative_usage_or_a_nearby_incomplete_record(self):
        def at(seconds):
            return (NOW - timedelta(minutes=1) + timedelta(seconds=seconds)).isoformat()

        self.write(".codex/sessions/2026/11/25/mirrors.jsonl",
                   codex_usage(at(0), 5, "first", 5),
                   codex_legacy(at(1), 5, None),
                   codex_legacy(at(2), 5, None),
                   codex_usage(at(4), 7, "second", 12),
                   codex_legacy(at(5), 7, 99),
                   codex_usage(at(8), 11, "third", None),
                   codex_legacy(at(9), 11, 111))
        self.assertEqual(self.read(0)["days"], [(20261125, 35, 1)])

    def test_codex_archive_move_and_copy_keep_one_rollout(self):
        active = ".codex/sessions/2026/11/25/rollout.jsonl"
        archived = ".codex/archived_sessions/rollout.jsonl"
        stamp = "2026-11-25T05:00:00Z"
        meta = {"type": "session_meta", "payload": {"id": "archived-session"}}
        usage = codex_legacy(stamp, 12, 12)
        path = self.write(active, meta, usage)
        before = self.read(0)
        copy = self.write(archived, meta, usage)
        self.assertEqual(self.read(0), before)
        path.unlink()
        self.assertEqual(self.read(0), before)
        copy.unlink()
        self.assertEqual(self.read(0), before)

        self.write(".codex/archived_sessions/only-archived.jsonl", codex_legacy(stamp, 19, 19))
        self.assertEqual(self.read(0)["days"], [(20261125, 31, 2)])

    def test_codex_upgrade_replaces_cached_rows_after_archive_rename(self):
        stamp = "2026-11-25T05:00:00Z"
        name = ".codex/sessions/2026/11/25/rollout.jsonl"
        modern = codex_usage(stamp, 30, "response", 35)
        modern["payload"].update(thread_id="move", turn_id="turn")
        modern["payload"]["usage"].update(input_tokens=1_300, cached_input_tokens=1_100)
        legacy = codex_legacy(stamp, 25, 30)
        legacy["payload"]["info"]["last_token_usage"].update(input_tokens=1_200, cached_input_tokens=1_000)
        path = self.write(name, {"type": "session_meta", "payload": {"id": "move", "forked_from_id": "parent"}},
                          {"type": "session_meta", "payload": {"id": "parent"}}, modern, legacy)
        expected = self.read(0)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            # An older collector switched to the embedded parent header.
            db.execute("DELETE FROM codex_rollout_cursors")
            db.execute("UPDATE events SET session='parent' WHERE file=?", (str(path),))
            db.execute("UPDATE event_inputs SET session='parent' WHERE file=?", (str(path),))
        archive = self.home / ".codex/archived_sessions" / path.name
        archive.parent.mkdir(parents=True)
        path.rename(archive)
        self.assertEqual(self.read(0), expected)

    def test_codex_full_dual_format_replaces_one_usage_after_append(self):
        name = ".codex/sessions/2026/11/25/dual.jsonl"
        modern = codex_usage("2026-11-25T05:00:00Z", 30, "response", 70)
        modern["payload"].update(thread_id="session", turn_id="turn")
        modern["payload"]["usage"].update(input_tokens=1_300, cached_input_tokens=1_100)
        modern["payload"]["thread_token_usage"].update(input_tokens=1_800, cached_input_tokens=1_400)
        self.write(name, {"type": "session_meta", "payload": {"id": "session"}}, modern)
        self.assertEqual(self.read(0)["days"], [(20261125, 30, 1)])
        legacy = codex_legacy("2026-11-25T05:00:45Z", 25, 65)
        legacy["payload"]["info"]["last_token_usage"].update(input_tokens=1_200, cache_read_input_tokens=1_000)
        self.write(name, legacy, append=True)
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 25, 1)])
        self.assertEqual(result["day_usage"], {20261125: (1_200, 1_000)})
        self.assertEqual(self.read(0), result)

    def test_codex_cumulative_only_deltas_continue_from_saved_baseline(self):
        name = ".codex/archived_sessions/cumulative.jsonl"
        first = codex_legacy("2026-11-24T15:59:59Z", 5, 5)
        first_info = first["payload"]["info"]
        first_info["last_token_usage"].update(input_tokens=100, cached_input_tokens=60)
        first_info["total_token_usage"].update(input_tokens=100, cached_input_tokens=60)
        self.write(name, first)
        self.assertEqual(self.read(0)["days"], [(20261124, 5, 1)])
        later = codex_legacy("2026-11-24T16:00:01Z", 0, 12)
        later_info = later["payload"]["info"]
        del later_info["last_token_usage"]
        later_info["total_token_usage"].update(input_tokens=150, cached_input_tokens=90)
        self.write(name, later, later, append=True)
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 7, 1), (20261124, 5, 1)])
        self.assertEqual(result["day_usage"], {20261125: (50, 30), 20261124: (100, 60)})

    def test_codex_fork_replay_keeps_only_new_child_usage(self):
        name = ".codex/sessions/2026/11/25/child.jsonl"
        start = datetime(2026, 11, 25, 5, tzinfo=timezone.utc)
        child = {"type": "session_meta", "timestamp": start.isoformat(),
                 "payload": {"id": "child", "forked_from_id": "parent"}}
        parent = {"type": "session_meta", "timestamp": start.isoformat(), "payload": {"id": "parent"}}
        replay = []
        for count in range(1, 21):
            entry = codex_legacy((start + timedelta(milliseconds=count * 10)).isoformat(), 1, count)
            info = entry["payload"]["info"]
            info["last_token_usage"].update(input_tokens=10, cached_input_tokens=7)
            info["total_token_usage"].update(input_tokens=count * 10, cached_input_tokens=count * 7)
            replay.append(entry)

        self.write(name, child, parent, *replay[:10])
        self.assertEqual(self.read(0)["days"], [(20261125, 10, 1)])
        self.write(name, *replay[10:], append=True)
        self.assertEqual(self.read(0)["days"], [])

        recorded = codex_legacy((start + timedelta(seconds=3)).isoformat(), 0, 27)
        info = recorded["payload"]["info"]
        del info["last_token_usage"]
        info["total_token_usage"].update(input_tokens=250, cached_input_tokens=170)
        self.write(name, recorded, append=True)
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 7, 1)])
        self.assertEqual(result["day_usage"], {20261125: (50, 30)})
        from deskpet_platform.notifications import NotificationStore
        self.assertEqual(result["sessions"][0]["key"],
                         NotificationStore._session_key("codex", "child").hex())

    def test_session_projection_uses_latest_title_and_execution_state(self):
        name = ".codex/sessions/2026/11/25/session.jsonl"

        def at(seconds):
            return (NOW + timedelta(seconds=seconds)).isoformat()

        def event(kind, seconds, **fields):
            return {"type": "event_msg", "timestamp": at(seconds), "payload": {"type": kind, **fields}}

        self.write(name, event("thread_name_updated", -25, thread_name="Old"),
                   codex_usage(at(-20), 9, "first", 9),
                   event("thread_name_updated", -15, thread_name="Current"),
                   event("task_started", -8, turn_id="turn"))
        session, = self.read(0)["sessions"]
        self.assertEqual((session["title"], session["state"], session["turns"]), ("Current", 1, 1))

        self.write(name, event("task_complete", -8, turn_id="turn"), append=True)
        session, = self.read(0)["sessions"]
        self.assertEqual((session["title"], session["state"], session["turns"]), ("Current", 0, 1))

    def test_usage_backfill_weights_cache_and_preserves_unavailable_history(self):
        name = ".codex/sessions/2026/11/25/live.jsonl"
        missing = ".codex/sessions/2026/11/24/missing.jsonl"
        first = codex_usage("2026-11-25T05:00:00Z", 1_726_916, "first", 1_726_916)
        first["payload"]["usage"].update(input_tokens=1_163_035_646, cached_input_tokens=1_150_841_088,
                                          reasoning_output_tokens=600_755, total_tokens=1_164_762_562)
        second = codex_usage("2026-11-25T05:00:01Z", 12, "second", 1_726_928)
        second["payload"]["usage"].update(input_tokens=400, cached_input_tokens=0)
        self.write(name, first, first, second, codex_legacy("2026-11-25T05:00:01Z", 12, 1_726_928))
        self.write(missing, codex_usage("2026-11-24T05:00:00Z", 4, "gone", 4))
        self.store.collect(self.now)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            # What the output-only receiver left behind after reading both files.
            db.execute("DELETE FROM event_inputs")
            db.execute("DELETE FROM usage_cursors")
        (self.home / missing).unlink()
        result = self.read(0)
        self.assertEqual(result["days"], [(20261125, 1_726_928, 1), (20261124, 4, 1)])
        self.assertEqual(result["day_usage"], {20261125: (1_163_036_046, 1_150_841_088),
                                              20261124: (None, None)})
        self.assertEqual(list(result["session_usage"].values()), [(1_163_036_046, 1_150_841_088), (None, None)])
        self.assertEqual(result, self.read(0))
        first["payload"]["usage"]["cached_input_tokens"] = 1
        self.write(name, first)
        self.assertEqual(self.read(0)["day_usage"][20261125], (1_163_035_646, 1))

    def test_incomplete_inputs_and_explicit_zero_are_distinct(self):
        name = ".codex/sessions/2026/11/25/live.jsonl"
        first = codex_usage("2026-11-25T05:00:00Z", 2, "first", 2)
        first["payload"]["usage"].update(input_tokens=40, cached_input_tokens=0)
        self.write(name, first)
        self.assertEqual(self.read(0)["day_usage"], {20261125: (40, 0)})
        second = codex_usage("2026-11-25T05:00:01Z", 2, "second", 4)
        second["payload"]["usage"].update(input_tokens=20, cached_input_tokens=21)
        self.write(name, second, append=True)
        self.assertEqual(self.read(0)["day_usage"], {20261125: (60, None)})
        self.write(name, codex_usage("2026-11-25T05:00:02Z", 2, "third", 6), append=True)
        result = self.read(0)
        self.assertEqual(result["day_usage"], {20261125: (None, None)})
        self.assertEqual(result["days"], [(20261125, 6, 1)])

    def test_real_schema_projections_keep_measured_zero_and_known_provider(self):
        stamp = "2026-11-25T08:00:00Z"
        for amount in (0, 3):
            self.write(".claude/projects/p/s.jsonl", {"type": "assistant", "timestamp": stamp, "sessionId": "s",
                       "message": {"id": "message", "usage": {"output_tokens": amount}}}, append=True)
        self.write(".claude/projects/p/zero.jsonl", {"type": "assistant", "timestamp": "2026-11-23T08:00:00Z",
                   "message": {"id": "zero", "usage": {"output_tokens": 0, "input_tokens": 0,
                                                        "cache_creation_input_tokens": 0,
                                                        "cache_read_input_tokens": 0}}})
        self.write(".claude/projects/p/other.jsonl", {"type": "assistant", "timestamp": "2026-11-22T08:00:00Z",
                   "message": {"id": "other", "usage": {"output_tokens": 1, "input_tokens": 2,
                                                         "cache_creation_input_tokens": 3,
                                                         "cache_read_input_tokens": 10}}})
        claude = self.read(1)
        self.assertEqual(claude["days"], [(20261125, 3, 1), (20261123, 0, 1), (20261122, 1, 1)])
        self.assertEqual(claude["day_usage"], {20261125: (None, None), 20261123: (0, 0), 20261122: (15, 10)})
        ag = {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": stamp}
        self.write(".gemini/antigravity/brain/task/.system_generated/logs/transcript.jsonl",
                   dict(ag, type="USER_INPUT", source="USER_EXPLICIT", step_index=0),
                   dict(ag, model="gemini-4-argon-high", output_tokens=11, input_tokens=5,
                        cache_read_tokens=60, step_index=1),
                   dict(ag, model="claude-opus-5-5-high", output_tokens=17, step_index=2),
                   dict(ag, model=None, output_tokens=23, step_index=3))
        gemini = self.read(2)
        self.assertEqual(gemini["days"], [(20261125, 11, 1)])
        self.assertEqual(gemini["day_usage"], {20261125: (65, 60)})
        self.assertEqual(gemini["sessions"][0]["turns"], 1)
        self.assertEqual(self.read(3)["days"], [(20261125, 17, 1)])
        wb = {"type": "message", "role": "assistant", "timestamp": int(self.now * 1000), "sessionId": "wb",
              "providerData": {"messageId": "response"}, "message": {"usage": {
                  "output_tokens": 19, "input_tokens": 82, "cache_read_input_tokens": 61, "total_tokens": 101}}}
        self.write(".workbuddy/projects/p/wb.jsonl", wb, dict(wb, type="function_call"))
        workbuddy = self.read(5)
        self.assertEqual(workbuddy["days"], [(20261125, 19, 1)])
        self.assertEqual(workbuddy["day_usage"], {20261125: (82, 61)})

    def test_antigravity_uses_native_step_models_and_reclassifies_saved_rows(self):
        root = self.home / ".gemini/antigravity"
        transcript = ".gemini/antigravity/brain/activity/.system_generated/logs/transcript.jsonl"
        stamp = "2026-11-25T08:00:00Z"
        rows = {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": stamp,
                "input_tokens": 5, "cache_read_tokens": 20}
        self.write(transcript,
                   {"type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": stamp, "step_index": 0},
                   dict(rows, step_index=1, model=None, output_tokens=11),
                   dict(rows, step_index=2, model="gemini-4-argon-high", output_tokens=17))
        self.assertEqual(self.read(2)["days"], [(20261125, 17, 1)])
        self.assertEqual(self.read(3)["days"], [])

        def generation(step, model):
            return _protobuf_blob(1, _protobuf_blob(19, model.encode())) + bytes([16, step])

        db_path = root / "conversations/activity.db"
        db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE gen_metadata(idx INTEGER, size INTEGER, data BLOB)")
            first = generation(1, "gemini-4-argon")
            second = generation(2, "claude-opus-5-5-medium")
            db.executemany("INSERT INTO gen_metadata VALUES (?,?,?)",
                           [(0, len(first), first), (1, len(second), second)])
        side_by_side = self.read(2)
        self.assertEqual(side_by_side["days"], [(20261125, 11, 1)])
        self.assertEqual(side_by_side["day_usage"], {20261125: (25, 20)})
        self.assertEqual(side_by_side["sessions"][0]["turns"], 1)
        self.assertEqual(self.read(3)["days"], [(20261125, 17, 1)])
        with closing(sqlite3.connect(db_path)) as db, db:
            corrected = generation(2, "gemini-4-carbon")
            db.execute("UPDATE gen_metadata SET size=?,data=? WHERE idx=1", (len(corrected), corrected))
        self.assertEqual(self.read(2)["days"], [(20261125, 28, 1)])
        self.assertEqual(self.read(3)["days"], [])

    def test_antigravity_buckets_usage_by_native_completion_across_midnight(self):
        stamp = "2026-11-24T15:59:59Z"
        transcript = ".gemini/antigravity/brain/midnight/.system_generated/logs/transcript.jsonl"
        self.write(transcript, {"type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": stamp,
                                "step_index": 1, "model": "gemini-4-argon-high", "input_tokens": 5,
                                "cache_read_tokens": 20, "output_tokens": 9})
        self.assertEqual(self.read(2)["days"], [(20261124, 9, 1)])
        db_path = self.home / ".gemini/antigravity/conversations/midnight.db"
        db_path.parent.mkdir(parents=True)
        finish = int(datetime(2026, 11, 24, 16, 0, 1, tzinfo=timezone.utc).timestamp())
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE steps(idx INTEGER, step_type INTEGER, status INTEGER, metadata BLOB)")
            db.execute("INSERT INTO steps VALUES (1,15,3,?)", (_protobuf_blob(7, b"\x08" + _varint(finish)),))
        self.assertEqual(self.read(2)["days"], [(20261125, 9, 1)])

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
            db.execute("UPDATE message SET data=?", (json.dumps({"role": "assistant", "tokens": {
                "output": 14, "reasoning": 6, "input": 55, "cache": {"read": 70, "write": 10}, "total": 155}}),))
            db.commit()
            opencode = self.read(4)
            self.assertEqual(opencode["sessions"][0]["output_tokens"], 20)
            self.assertEqual(opencode["day_usage"], {20261125: (135, 70)})

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
