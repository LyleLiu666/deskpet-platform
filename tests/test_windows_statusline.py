import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "nt", "Windows status line rehearsal")
class WindowsStatuslineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="DeskPet 状态 with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "settings.json"
        self.store = self.root / "store" / "quota.sqlite3"
        self.old = self.root / "original status.py"
        self.old.write_text("import sys\ndata=sys.stdin.buffer.read()\nsys.stdout.buffer.write(b'original:' + data)\nsys.stderr.write('original-error')\n",encoding="utf-8")
        self.input = json.dumps({"private":"fixture 私密", "rate_limits":{"five_hour":{
            "used_percentage":20,"resets_at":int(time.time())+600}}},ensure_ascii=False).encode("utf-8")

    def do_rehearsal(self, source, shell, old_command, parent, *, executable=None):
        from deskpet_platform.quota_statusline import configure
        from deskpet_platform.quota_capture import QuotaStore
        original = {"other":True,"statusLine":{"type":"command","command":old_command,"padding":2}}
        self.settings.write_text(json.dumps(original),encoding="utf-8")
        self.assertTrue(configure(self.settings,source,self.store,enabled=True,executable=executable,windows_shell=shell))
        self.assertFalse(configure(self.settings,source,self.store,enabled=True,executable=executable,windows_shell=shell))
        command = json.loads(self.settings.read_text(encoding="utf-8"))["statusLine"]["command"]
        if shell[0] == "cmd":
            # Pass a raw command line: Python's list quoting adds backslashes
            # that cmd.exe does not treat as quote escapes.
            baseline_args = f'"{parent[0]}" /d /s /c "{old_command}"'
        else:
            baseline_args = [*parent, old_command]
        baseline = subprocess.run(baseline_args, input=self.input, capture_output=True, timeout=15)
        result = subprocess.run([*parent, command], input=self.input, capture_output=True, timeout=15)
        self.assertEqual((result.returncode,result.stdout,result.stderr),
                         (baseline.returncode,baseline.stdout,baseline.stderr))
        self.assertEqual(baseline.returncode, 0)
        if executable is None:
            reading = QuotaStore(self.store).read(source,time.time())
            self.assertIsNotNone(reading)
            if source == "claude_code":
                self.assertEqual(reading["quotas"][0][:2],[80,300])
        self.assertTrue(configure(self.settings,source,self.store,enabled=False,windows_shell=shell))
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),original)

    def test_does_not_replace_settings_when_windows_powershell_is_unavailable(self):
        from unittest.mock import patch
        from deskpet_platform.quota_statusline import configure
        original = {"statusLine": {"type": "command", "command": "echo old"}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        with patch("deskpet_platform.quota_statusline.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "PowerShell"):
                configure(self.settings, "antigravity", self.store, enabled=True,
                          windows_shell=("cmd", os.environ.get("COMSPEC", "cmd.exe")))
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), original)

    def test_existing_settings_acl_survives_install_and_remove(self):
        from deskpet_platform.quota_statusline import configure
        from deskpet_platform.windows_private import current_user_sid
        from test_windows_private import dacl_trustees
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo old"}}), encoding="utf-8")
        subprocess.run(["icacls", str(self.settings), "/inheritance:r", "/grant:r",
                        "*" + current_user_sid() + ":F", "*S-1-5-18:F"], check=True, capture_output=True)
        before = sorted(dacl_trustees(self.settings))
        shell = ("cmd", os.environ.get("COMSPEC", "cmd.exe"))
        configure(self.settings, "antigravity", self.store, enabled=True, windows_shell=shell)
        self.assertEqual(sorted(dacl_trustees(self.settings)), before)
        configure(self.settings, "antigravity", self.store, enabled=False, windows_shell=shell)
        self.assertEqual(sorted(dacl_trustees(self.settings)), before)

    def test_previous_failure_status_and_stderr_are_preserved(self):
        from deskpet_platform.quota_statusline import configure
        script = self.root / "previous failure.py"
        script.write_text("import sys; sys.stdout.buffer.write(b'old'); sys.stderr.buffer.write(b'err'); sys.exit(7)", encoding="utf-8")
        original = subprocess.list2cmdline([sys.executable, str(script)])
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": original}}), encoding="utf-8")
        shell = ("cmd", os.environ.get("COMSPEC", "cmd.exe"))
        configure(self.settings, "antigravity", self.store, enabled=True, windows_shell=shell)
        command = json.loads(self.settings.read_text(encoding="utf-8"))["statusLine"]["command"]
        result = subprocess.run([shell[1], "/d", "/s", "/c", command], input=self.input, capture_output=True, timeout=15)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b"old", b"err"))

    def test_claude_with_git_bash_keeps_the_previous_status_line(self):
        bash = shutil.which("bash.exe")
        if not bash:
            self.skipTest("Git Bash not installed")
        previous = f"{shlex.quote(sys.executable.replace(chr(92), '/'))} {shlex.quote(str(self.old).replace(chr(92), '/'))}"
        self.do_rehearsal("claude_code",("bash",bash),previous,[bash,"-c"])

    def test_claude_with_powershell_keeps_the_previous_status_line(self):
        powershell = shutil.which("powershell.exe")
        previous = "& '" + sys.executable.replace("'", "''") + "' '" + str(self.old).replace("'", "''") + "'"
        self.do_rehearsal("claude_code",("powershell",powershell),previous,[powershell,"-NoProfile","-NonInteractive","-Command"])

    def test_antigravity_with_cmd_keeps_the_previous_status_line(self):
        cmd = os.environ.get("COMSPEC","cmd.exe")
        previous = subprocess.list2cmdline([sys.executable,str(self.old)])
        self.do_rehearsal("antigravity",("cmd",cmd),previous,[cmd,"/d","/s","/c"])

    def test_missing_capture_program_falls_back_to_previous_status_line(self):
        cmd = os.environ.get("COMSPEC","cmd.exe")
        previous = subprocess.list2cmdline([sys.executable,str(self.old)])
        self.do_rehearsal("antigravity",("cmd",cmd),previous,[cmd,"/d","/s","/c"],executable=self.root / "missing python.exe")
