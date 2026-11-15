import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from deskpet_platform.quota_capture import MAX_INPUT_BYTES, QuotaStore


class QuotaCaptureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = QuotaStore(self.root / "quota.sqlite3")
        self.now = 1_800_000_000
        self.claude = {"email": "secret@example.test", "transcript_path": "/private/conversation",
                       "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": self.now + 60}}}

    def test_stores_only_quota_information_and_keeps_sources_separate(self):
        self.store.record("claude_code", self.claude, self.now)
        self.store.record("antigravity", {"email": "other@example.test", "quota": {
            "gemini-weekly": {"remaining_fraction": 0.7}}}, self.now + 1)
        self.assertEqual(self.store.read("claude_code", self.now + 2), {"quotas": [[80, 300, self.now + 60]]})
        self.assertEqual(self.store.read("antigravity", self.now + 2), {
            "gemini-weekly": {"remaining_percent": 70, "reset_at": None}})
        raw = self.store.path.read_bytes()
        for private in (b"secret@example.test", b"other@example.test", b"/private/conversation"):
            self.assertNotIn(private, raw)

    def test_missing_new_reading_clears_old_data_and_older_writes_cannot_replace_newer(self):
        self.store.record("claude_code", self.claude, self.now)
        self.store.record("claude_code", {}, self.now + 2)
        self.store.record("claude_code", self.claude, self.now + 1)
        self.assertEqual(self.store.read("claude_code", self.now + 3), {"quotas": []})

    def test_does_not_report_old_or_expired_data_as_current(self):
        self.store.record("claude_code", self.claude, self.now)
        self.assertEqual(self.store.read("claude_code", self.now + 60), {"quotas": []})
        self.assertIsNone(self.store.read("claude_code", self.now + 301))
        self.assertIsNone(self.store.read("claude_code", self.now - 1))
        self.assertIsNone(self.store.read("antigravity", self.now))

    def test_capture_passes_input_unchanged_to_existing_statusline(self):
        now = int(time.time())
        payload = {**self.claude, "rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": now + 600}}}
        data = json.dumps(payload).encode()
        capture = subprocess.run([sys.executable, "-m", "deskpet_platform.quota_capture", "claude_code",
                                  str(self.store.path)], input=data, capture_output=True, check=True)
        self.assertEqual(capture.stdout, data)
        self.assertEqual(capture.stderr, b"")
        self.assertEqual(self.store.read("claude_code", time.time()), {"quotas": [[80, 300, now + 600]]})

    def test_existing_statusline_can_still_process_its_original_input(self):
        now = int(time.time())
        payload = {"private": "keep this input", "rate_limits": {
            "seven_day": {"used_percentage": 75, "resets_at": now + 600}}}
        data = json.dumps(payload).encode()
        capture = subprocess.Popen([sys.executable, "-m", "deskpet_platform.quota_capture", "claude_code",
                                    str(self.store.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            previous = subprocess.Popen([sys.executable, "-c",
                                         "import json,sys; value=json.load(sys.stdin); "
                                         "print('existing status: '+value['private'])"],
                                        stdin=capture.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            capture.stdout.close()
            capture.stdin.write(data)
            capture.stdin.close()
            output, error = previous.communicate(timeout=5)
            self.assertEqual(previous.returncode, 0, error)
            self.assertEqual(output, ("existing status: keep this input" + os.linesep).encode())
            self.assertEqual(capture.wait(timeout=5), 0)
            self.assertEqual(self.store.read("claude_code", time.time()), {"quotas": [[25, 10080, now + 600]]})
        finally:
            if capture.poll() is None:
                capture.kill()
                capture.wait()

    def test_capture_does_not_break_existing_statusline_when_storage_fails(self):
        data = b'{"rate_limits":{}, "private":"input remains intact"}'
        bad_store = self.root / "directory"
        bad_store.mkdir()
        capture = subprocess.run([sys.executable, "-m", "deskpet_platform.quota_capture", "claude_code",
                                  str(bad_store)], input=data, capture_output=True, check=True)
        self.assertEqual(capture.stdout, data)
        self.assertEqual(capture.stderr, b"")

    def test_bad_or_oversized_statusline_input_still_reaches_original_command(self):
        for data in (b"not json", b"x" * (MAX_INPUT_BYTES + 17)):
            with self.subTest(size=len(data)):
                capture = subprocess.run([sys.executable, "-m", "deskpet_platform.quota_capture", "claude_code",
                                          str(self.store.path)], input=data, capture_output=True, check=True)
                self.assertEqual(capture.stdout, data)
                self.assertEqual(capture.stderr, b"")
                self.assertFalse(self.store.path.exists())

    def test_capture_never_writes_through_a_symlink(self):
        target = self.root / "existing-file"
        target.write_bytes(b"keep")
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.store.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable to this user")
        with self.assertRaises(ValueError):
            self.store.record("claude_code", self.claude, self.now)
        self.assertEqual(target.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
