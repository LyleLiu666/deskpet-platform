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
        result = subprocess.run([*parent, command], input=self.input, capture_output=True, timeout=15)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b"original:"+self.input,b"original-error"))
        if executable is None:
            reading = QuotaStore(self.store).read(source,time.time())
            self.assertIsNotNone(reading)
            if source == "claude_code":
                self.assertEqual(reading["quotas"][0][:2],[80,300])
        self.assertTrue(configure(self.settings,source,self.store,enabled=False,windows_shell=shell))
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),original)

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
