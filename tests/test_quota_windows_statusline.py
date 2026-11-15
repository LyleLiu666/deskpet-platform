import unittest
import base64
from deskpet_platform.quota_statusline import _command, _metadata


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

    def test_an_unrelated_encoded_powershell_command_is_not_claimed(self):
        encoded = base64.b64encode("Write-Output hello".encode("utf-16le")).decode("ascii")
        self.assertIsNone(_metadata("powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded))

    def test_excessively_long_command_is_rejected(self):
        value = self.metadata("cmd")
        value["original_command"] = "a" * 30000
        with self.assertRaisesRegex(ValueError, "过长"):
            _command(value)
