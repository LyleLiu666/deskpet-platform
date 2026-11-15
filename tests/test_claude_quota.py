import unittest

from deskpet_platform.claude_quota import read_statusline_quota


class ClaudeQuotaTests(unittest.TestCase):
    def test_subscriber_windows_are_remaining_not_context_or_used_percentage(self):
        payload = {
            "context_window": {"used_percentage": 98},
            "rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 1_800_001_000},
                "seven_day": {"used_percentage": 87, "resets_at": 1_800_010_000},
            },
        }
        self.assertEqual(read_statusline_quota(payload, 1_800_000_000), {
            "quotas": [[76, 300, 1_800_001_000], [13, 10080, 1_800_010_000]],
        })

    def test_missing_or_ineligible_data_never_becomes_full_allowance(self):
        for payload in (None, {}, {"rate_limits": None}, {"rate_limits": {}},
                        {"context_window": {"used_percentage": 0}}):
            with self.subTest(payload=payload):
                self.assertEqual(read_statusline_quota(payload, 1_800_000_000), {"quotas": []})

    def test_windows_are_independent_and_expired_ones_are_omitted(self):
        now = 1_800_000_000
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 100, "resets_at": now + 1},
            "seven_day": {"used_percentage": 4, "resets_at": now},
        }}
        self.assertEqual(read_statusline_quota(payload, now), {"quotas": [[0, 300, now + 1]]})
        payload["rate_limits"]["seven_day"]["resets_at"] = now + 60
        payload["rate_limits"]["five_hour"] = None
        self.assertEqual(read_statusline_quota(payload, now), {"quotas": [[96, 10080, now + 60]]})

    def test_invalid_fields_are_ignored_without_hiding_valid_other_window(self):
        now = 1_800_000_000
        invalid = (True, -0.01, 100.01, float("inf"), float("nan"), 10**1000, "12", None)
        for value in invalid:
            with self.subTest(value=value):
                payload = {"rate_limits": {
                    "five_hour": {"used_percentage": value, "resets_at": now + 100},
                    "seven_day": {"used_percentage": 0, "resets_at": now + 200},
                }}
                self.assertEqual(read_statusline_quota(payload, now), {"quotas": [[100, 10080, now + 200]]})

    def test_reset_must_be_a_future_integer_supported_by_the_device(self):
        now = 1_800_000_000
        for reset in (True, "1800001000", 1_800_001_000.0, now - 1, now, 0x1_0000_0000):
            with self.subTest(reset=reset):
                payload = {"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": reset}}}
                self.assertEqual(read_statusline_quota(payload, now), {"quotas": []})


if __name__ == "__main__":
    unittest.main()
