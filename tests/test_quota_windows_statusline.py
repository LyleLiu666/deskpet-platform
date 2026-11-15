import unittest
import base64
import json
import os
import shlex
import gzip
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from deskpet_platform.quota_statusline import MARKER, _command, _metadata
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

    def test_posix_marker_cannot_spoof_windows_metadata(self):
        raw = json.dumps(self.metadata("cmd"), separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        with self.assertRaises(ValueError):
            _metadata(MARKER + token + "\necho unrelated")

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

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required for the proxy rehearsal")
    def test_stalled_capture_does_not_block_existing_statusline(self):
        powershell = shutil.which("pwsh")
        with tempfile.TemporaryDirectory(prefix="deskpet stalled capture ") as directory:
            root = Path(directory)
            package = root / "deskpet_platform"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "quota_capture.py").write_text("import time; time.sleep(30)", encoding="utf-8")
            old = root / "previous.py"
            old.write_text("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", encoding="utf-8")
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            value = self.metadata("powershell")
            value.update(store=str(root / "quota.sqlite3"), executable=sys.executable,
                         original_command=f"& {quote(sys.executable)} {quote(old)}",
                         windows_shell=["powershell", powershell])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root)
            payload = b'{"fixture":"preserved"}'
            encoded = _command(value)[len(PREFIX):]
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    input=payload, capture_output=True, timeout=3, env=env)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, payload, b""))

    @unittest.skipUnless(os.name != "nt" and shutil.which("pwsh") and shutil.which("bash"),
                         "a POSIX Bash and PowerShell are required for this proxy rehearsal")
    def test_bash_parent_keeps_original_input_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="deskpet bash proxy ") as directory:
            root = Path(directory)
            old = root / "previous.py"
            old.write_text("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write('old-error'); sys.exit(7)", encoding="utf-8")
            (root / "powershell.exe").symlink_to(shutil.which("pwsh"))
            value = self.metadata("bash")
            value.update(store=str(root / "quota.sqlite3"), executable=sys.executable,
                         original_command=shlex.join([sys.executable, str(old)]),
                         windows_shell=["bash", shutil.which("bash")])
            data = b'{"private":"keep binary input"}'
            env = os.environ.copy()
            env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([shutil.which("bash"), "-c", _command(value)], input=data,
                                    capture_output=True, timeout=15, env=env)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (7, data, b"old-error"))

    def test_excessively_long_command_is_rejected(self):
        value = self.metadata("cmd")
        value["original_command"] = "a" * 30000
        with self.assertRaisesRegex(ValueError, "过长"):
            _command(value)
