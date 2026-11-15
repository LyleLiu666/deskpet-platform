import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows private storage")
class WindowsPrivateStorageTests(unittest.TestCase):
    def test_dedicated_directory_and_new_files_are_restricted_and_writable(self):
        from deskpet_platform.windows_private import current_user_sid, ensure_private_directory
        with tempfile.TemporaryDirectory(prefix="deskpet-private-") as directory:
            root = Path(directory) / ".deskpet-private"
            ensure_private_directory(root)
            path = root / "fixture.txt"
            path.write_text("synthetic content", encoding="utf-8")
            ensure_private_directory(root)
            self.assertEqual(path.read_text(encoding="utf-8"), "synthetic content")
            script = r'''param($Path)
$entries = (Get-Acl -LiteralPath $Path).Access | Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object {
  $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
}
@($entries) | ConvertTo-Json -Compress
'''
            probe = Path(directory) / "read-acl.ps1"
            probe.write_text(script, encoding="utf-8")
            for item in (root, path):
                result = subprocess.run(["pwsh", "-NoProfile", "-File", str(probe), str(item)],
                                        check=True, text=True, capture_output=True, timeout=15)
                self.assertTrue(result.stdout.strip(), result.stderr)
                sids = json.loads(result.stdout)
                if isinstance(sids, str):
                    sids = [sids]
                self.assertIn(current_user_sid(), sids)
                self.assertTrue(set(sids) <= {current_user_sid(), "S-1-5-18"}, sids)
