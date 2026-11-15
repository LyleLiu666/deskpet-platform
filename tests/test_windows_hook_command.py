import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SHELL = shutil.which("powershell.exe" if os.name == "nt" else "pwsh")


@unittest.skipUnless(SHELL, "PowerShell is required for hook proxy rehearsal")
class WindowsHookCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="DeskPet hook & spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "fake hook.py"
        self.record = self.root / "input.json"
        self.script.write_text("import pathlib,sys\n"
                               "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\n"
                               "sys.stdout.write('{\\\"decision\\\":\\\"allow\\\"}\\n')\n", encoding="utf-8")

    def run_hook(self, executable, *args):
        from deskpet_platform.windows_hook_command import build
        command = build(executable, list(args))
        encoded = command.rsplit(" ", 1)[1]
        payload = json.dumps({"session_id": "fixture", "title": "中文 & spaces"}, ensure_ascii=False).encode()
        result = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                input=payload, capture_output=True, timeout=4)
        return payload, result

    def test_forwards_raw_input_and_response_without_shell_interpretation(self):
        payload, result = self.run_hook(sys.executable, str(self.script), str(self.record))
        self.assertEqual(self.record.read_bytes(), payload)
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})
        self.assertEqual(result.returncode, 0)

    def test_missing_executable_returns_a_nonblocking_response(self):
        _, result = self.run_hook(str(self.root / "missing executable"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_stalled_hook_does_not_block_agent(self):
        self.script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        _, result = self.run_hook(sys.executable, str(self.script))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})
