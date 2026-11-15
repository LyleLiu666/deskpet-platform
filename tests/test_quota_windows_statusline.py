import unittest
import base64
import json
import gzip
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from deskpet_platform.quota_statusline import _command, _metadata
from deskpet_platform.quota_windows_statusline import PREFIX, BOOT_PREFIX, BOOT_SUFFIX
from deskpet_platform.quota_capture import QuotaStore


class WindowsStatuslineEncodingTests(unittest.TestCase):
    def metadata(self, kind):
        return {"source": "claude_code", "store": r"C:\\Desk Pet\\配额.sqlite3",
                "executable": r"C:\\Desk Pet\\python.exe", "original_command": "echo '$x & 中文'",
                "had_status": True, "added_type": False, "added_stack": False,
                "windows_shell": [kind, r"C:\\Windows\\shell.exe"]}

    def test_reversible_without_shell_interpreting_user_paths(self):
        for shell in ("bash", "powershell", "cmd"):
            with self.subTest(shell=shell):
                value = self.metadata(shell)
                wrapped = _command(value)
                self.assertNotIn("中文", wrapped)
                self.assertEqual(_metadata(wrapped), value)
                with self.assertRaises(ValueError):
                    _metadata(wrapped[:-5] + "AAAAA")

    def test_other_valid_compression_levels_can_still_be_removed(self):
        value = self.metadata("cmd")
        wrapped = _command(value)
        bootstrap = base64.b64decode(wrapped[len(PREFIX):]).decode("utf-16le")
        compressed = base64.b64decode(bootstrap[len(BOOT_PREFIX):-len(BOOT_SUFFIX)])
        script = gzip.decompress(compressed)
        alternate = base64.b64encode(gzip.compress(script, compresslevel=1, mtime=0)).decode("ascii")
        encoded = base64.b64encode((BOOT_PREFIX + alternate + BOOT_SUFFIX).encode("utf-16le")).decode("ascii")
        self.assertEqual(_metadata(PREFIX + encoded), value)

    def test_an_unrelated_encoded_powershell_command_is_not_claimed(self):
        encoded = base64.b64encode("Write-Output hello".encode("utf-16le")).decode("ascii")
        self.assertIsNone(_metadata("powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required for the proxy rehearsal")
    def test_proxy_forwards_bytes_and_works_if_capture_program_is_missing(self):
        powershell = shutil.which("pwsh")
        with tempfile.TemporaryDirectory(prefix="deskpet proxy with spaces ") as directory:
            root = Path(directory)
            old = root / "previous.py"
            old.write_text("import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(b'old:'+data); sys.stderr.write('previous-error')", encoding="utf-8")
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            value = self.metadata("powershell")
            value.update(store=str(root / "quota.sqlite3"), executable=sys.executable,
                         original_command=f"& {quote(sys.executable)} {quote(old)}",
                         windows_shell=["powershell", powershell])
            payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 20,
                       "resets_at": int(time.time()) + 600}}, "private": "测试"}, ensure_ascii=False).encode()
            def run():
                encoded = _command(value)[len(PREFIX):]
                return subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                      input=payload, capture_output=True, timeout=15)
            result = run()
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, b"old:" + payload, b"previous-error"))
            self.assertEqual(QuotaStore(value["store"]).read("claude_code", time.time())["quotas"][0][:2], [80, 300])
            value["executable"] = str(root / "missing")
            result = run()
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, b"old:" + payload, b"previous-error"))

    def test_excessively_long_command_is_rejected(self):
        value = self.metadata("cmd")
        value["original_command"] = "a" * 30000
        with self.assertRaisesRegex(ValueError, "过长"):
            _command(value)
