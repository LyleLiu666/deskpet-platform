import unittest

from deskpet_platform.quota_projection import project_quota


class QuotaProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.codex = {"quotas": [[80, 300, self.now + 300], [44, 10080, self.now + 3000]],
                      "credits": "99.5", "reset_cards": 2}
        self.claude = {"quotas": [[25, 300, self.now + 300], [10, 10080, self.now + 3000]]}
        self.antigravity = {
            "gemini-5h": {"remaining_percent": 95, "reset_at": self.now + 300},
            "gemini-weekly": {"remaining_percent": 50, "reset_at": self.now + 3000},
            "3p-5h": {"remaining_percent": 1, "reset_at": self.now + 400},
            "3p-weekly": {"remaining_percent": 0, "reset_at": self.now + 4000},
            "gemini-argon-wtu-weekly": {"remaining_percent": 3, "reset_at": self.now + 3000},
        }

    def select(self, source, **overrides):
        values = {"codex": self.codex, "claude": self.claude, "antigravity": self.antigravity}
        values.update(overrides)
        return project_quota(source, now=self.now, **values)

    def test_switching_never_reuses_another_agents_percentages_or_codex_extras(self):
        self.assertEqual(self.select("codex"), {"source": "codex", **self.codex})
        self.assertEqual(self.select("claude_code"), {"source": "claude_code", **self.claude})
        self.assertEqual(self.select("antigravity_gemini"), {"source": "antigravity_gemini", "quotas": [
            [95, 300, self.now + 300], [50, 10080, self.now + 3000]]})
        self.assertEqual(self.select("antigravity_third_party"), {"source": "antigravity_third_party", "quotas": [
            [1, 300, self.now + 400], [0, 10080, self.now + 4000]]})

    def test_missing_data_is_explicitly_empty_not_previous_or_another_group(self):
        self.assertEqual(self.select("claude_code", claude=None), {"source": "claude_code", "quotas": []})
        self.assertEqual(self.select("antigravity_third_party", antigravity={
            "gemini-weekly": {"remaining_percent": 75, "reset_at": self.now + 3000}}),
            {"source": "antigravity_third_party", "quotas": []})

    def test_unknown_reset_and_native_count_are_not_fabricated(self):
        actual = self.select("antigravity_gemini", antigravity={
            "gemini-5h": {"remaining_percent": 50, "reset_at": None},
            "gemini-weekly": {"remaining_amount": 42, "reset_at": None}})
        self.assertEqual(actual, {"source": "antigravity_gemini", "quotas": [[50, 300, None]]})

    def test_invalid_or_expired_windows_do_not_reappear(self):
        actual = self.select("claude_code", claude={"quotas": [[101, 300, self.now + 20],
                                                                 [10, 10080, self.now],
                                                                 [0, 300, self.now + 100]]})
        self.assertEqual(actual, {"source": "claude_code", "quotas": [[0, 300, self.now + 100]]})

    def test_unknown_source_is_not_silently_mapped_to_codex(self):
        with self.assertRaises(ValueError):
            self.select("future_agent")


if __name__ == "__main__":
    unittest.main()
