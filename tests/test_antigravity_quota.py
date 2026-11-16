from datetime import datetime, timezone
import unittest

from deskpet_platform.antigravity_quota import read_statusline_quota


class AntigravityQuotaTests(unittest.TestCase):
    def test_native_usage_result_keeps_exact_ids_for_both_model_groups(self):
        from deskpet_platform.antigravity_quota import read_usage_buckets
        from deskpet_platform.quota_projection import project_quota

        now = 1_800_000_000
        fixture = {"status": "SUCCESS", "num_turns": 0, "command": {"name": "usage", "data": {
            "groups": [{"buckets": [{"id": "gemini-weekly", "remaining_fraction": .98, "reset_in_seconds": 3600}]},
                       {"buckets": [{"id": "3p-5h", "remaining_fraction": 1, "reset_in_seconds": 300}]}]}}}
        reading = read_statusline_quota({"quota": read_usage_buckets(fixture)}, now)
        self.assertEqual(project_quota("antigravity_gemini", antigravity=reading, now=now)["quotas"],
                         [[98, 10080, now + 3600]])
        self.assertEqual(project_quota("antigravity_third_party", antigravity=reading, now=now)["quotas"],
                         [[100, 300, now + 300]])
        fixture["num_turns"] = 1
        self.assertEqual(read_usage_buckets(fixture), {})


    def test_preserves_native_bucket_identity_and_remaining_percentage(self):
        now = 1_800_000_000
        reset = datetime.fromtimestamp(now + 3600, timezone.utc).isoformat().replace("+00:00", "Z")
        payload = {"product": "antigravity", "email": "private@example.test", "quota": {
            "gemini-weekly": {"remaining_fraction": 0.9378, "reset_time": reset},
            "other-bucket": {"remaining_fraction": 0.0, "reset_in_seconds": 60},
        }}
        self.assertEqual(read_statusline_quota(payload, now), {
            "gemini-weekly": {"remaining_percent": 94, "reset_at": now + 3600},
            "other-bucket": {"remaining_percent": 0, "reset_at": now + 60},
        })

    def test_count_without_a_denominator_is_not_a_percentage(self):
        self.assertEqual(read_statusline_quota({"quota": {"bucket": {"remaining_amount": 12}}}, 1_800_000_000),
                         {"bucket": {"remaining_amount": 12, "reset_at": None}})

    def test_unknown_reset_stays_unknown_and_expired_reset_is_not_refreshed(self):
        now = 1_800_000_000
        expired = datetime.fromtimestamp(now - 1, timezone.utc).isoformat()
        value = {"quota": {"unknown": {"remaining_fraction": 0.2},
                           "expired": {"remaining_fraction": 0.3, "reset_time": expired}}}
        self.assertEqual(read_statusline_quota(value, now), {"unknown": {"remaining_percent": 20, "reset_at": None}})

    def test_invalid_disabled_or_missing_bucket_data_is_not_fabricated(self):
        now = 1_800_000_000
        for value in (None, {}, {"quota": None}, {"quota": {"missing": {}}},
                      {"quota": {"disabled": {"remaining_fraction": 0.5, "disabled": True}}},
                      {"quota": {"bad": {"remaining_fraction": True}}},
                      {"quota": {"bad": {"remaining_fraction": float("nan")}}},
                      {"quota": {"bad": {"remaining_fraction": 10**1000}}},
                      {"quota": {"bad": {"remaining_fraction": 1.1}}}):
            with self.subTest(value=value):
                self.assertEqual(read_statusline_quota(value, now), {})


if __name__ == "__main__":
    unittest.main()
