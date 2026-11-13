from pathlib import Path
import tempfile
import unittest

from deskpet_platform.role_sources import list_role_sources


class RoleSourceTests(unittest.TestCase):
    def test_finds_replaceable_role_directories_without_following_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pack = root / "pet" / "pet-v2"
            pack.mkdir(parents=True)
            (pack / "frames.bin").write_bytes(b"PET1")
            (pack / "timeline-v2.bin").write_bytes(b"PTL1")
            timed = root / "timed" / "pet-v2"
            timed.mkdir(parents=True)
            (timed / "frames.bin").write_bytes(b"PET1")
            (timed / "timeline-v3.bin").write_bytes(b"PTL1")
            dance = root / "duo"
            dance.mkdir()
            (dance / "motion.bin").write_bytes(b"DNC1")
            invalid = root / "other"
            invalid.mkdir()
            (invalid / "motion.bin").write_bytes(b"NOT1")
            (root / "linked").symlink_to(root / "pet", target_is_directory=True)

            result = list_role_sources(str(root))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["directory"], str(root))
            self.assertEqual(result["roles"], [
                {"id": "duo", "path": str(dance), "format": "DNC1"},
                {"id": "pet", "path": str(root / "pet"), "format": "PET1"},
                {"id": "timed", "path": str(root / "timed"), "format": "PET1"},
            ])

    def test_requires_a_directory_and_supports_unconfigured_state(self):
        self.assertEqual(list_role_sources(None)["status"], "not_configured")
        with self.assertRaises(ValueError):
            list_role_sources("relative/path")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                list_role_sources(str(Path(directory) / "missing"))
