from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deskpet_platform.token_metrics import TokenMetrics
from deskpet_platform.codex_quota import quota_reading
from deskpet_platform.reference_collector import ReferenceCollector
from test_role_pack import ROOT


class TokenMetricsTests(unittest.TestCase):
    def test_reference_collector_keeps_fresh_aggregate_and_rejects_stale_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "host/token_collector/pyproject.toml"
            manifest.parent.mkdir(parents=True)
            manifest.touch()
            collector = ReferenceCollector(root)
            sample = {"captured_at_ms": 1000000,
                      "total": {"speed_state": "measured", "output_tps": 12.4, "active_sessions": 2}}
            reading = collector.normalize(sample, 1001)
            self.assertEqual((reading["rate10"], reading["sessions"], reading["trend"][-1]), (124, 2, 124))
            sample["total"]["active_sessions"] = None
            self.assertIsNone(collector.normalize(sample, 1001)["sessions"])
            self.assertIsNone(collector.normalize(sample, 1004))
            sample["total"]["speed_state"] = "unknown"
            self.assertIsNone(collector.normalize(sample, 1001))

    def test_account_limits_preserve_zero_credits_and_ignore_expired_windows(self):
        result = {"rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 300, "usedPercent": 100, "resetsAt": 1050},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 80, "resetsAt": 2000},
            "credits": {"balance": "0.00"},
        }}, "rateLimitResetCredits": {"availableCount": 0}}
        self.assertEqual(quota_reading(result, 1050),
                         {"quotas": [[20, 10080, 2000]], "credits": "0", "reset_cards": 0})

    def test_two_sessions_prefer_modern_usage_and_keep_independent_quota_windows(self):
        now = 1800000000
        def event(age, kind, payload):
            return {"timestamp": datetime.fromtimestamp(now - age, timezone.utc).isoformat(),
                    "type": kind, "payload": payload}

        modern = event(2, "token_usage_record", {"response_id": "one", "usage": {"output_tokens": 240}})
        legacy = event(1, "event_msg", {"type": "token_count",
                                        "info": {"last_token_usage": {"output_tokens": 999}},
                                        "rate_limits": {
                                            "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": now + 1000},
                                            "secondary": {"used_percent": 56, "window_minutes": 10080, "resets_at": now + 10000},
                                        }})
        second = event(12, "event_msg", {"type": "token_count", "info": {"last_token_usage": {"output_tokens": 125}}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            day = root / datetime.fromtimestamp(now, timezone.utc).strftime("%Y/%m/%d")
            day.mkdir(parents=True)
            for file, entries in (("first.jsonl", [modern, legacy]), ("second.jsonl", [modern, second])):
                path = day / file
                path.write_text("".join(json.dumps(entry) + "\n" for entry in entries) + "{partial")
                os.utime(path, (now, now))
            collector = TokenMetrics(root)
            reading = collector.sample(now)
            self.assertEqual(reading["sessions"], 2)
            self.assertEqual(reading["rate10"], 240)
            self.assertEqual(sum(reading["trend"]), 730)
            self.assertEqual(reading["quotas"], [[75, 300, now + 1000], [44, 10080, now + 10000]])
            idle = collector.sample(now + 70)
            self.assertEqual((idle["rate10"], idle["sessions"]), (0, 0))
