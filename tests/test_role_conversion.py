from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from deskpet_platform.role_install import prepare_role, validate_action_pack
from deskpet_platform.role_staging import stage_role


def make_dance(directory, *, timed_frames=2):
    width, height = 4, 3
    palette = b"\x00\xf8" + bytes(510)
    row = bytes([1, 0, width]) + bytes([0, 255]) * width
    frame = palette + row * height
    frames = [frame] * (timed_frames + 1)
    header_size = 20 + 4 + 16 * len(frames)
    entries, offset = [], header_size
    for index, data in enumerate(frames):
        duration = 100 if timed_frames > 2 and index == 1 else (50 if timed_frames > 2 else 200)
        entries.append(struct.pack("<IIIHH", offset, len(data), zlib.crc32(data), duration, 0))
        offset += len(data)
    header = struct.pack("<4s6HI", b"DNC1", 1, width, height, 1, len(frames), 1, offset)
    (directory / "motion.bin").write_bytes(header + struct.pack("<HH", 0, timed_frames) + b"".join(entries) + b"".join(frames))


class RoleConversionTests(unittest.TestCase):
    def test_dance_is_converted_to_the_same_discoverable_role_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "duo"
            source.mkdir()
            make_dance(source)
            staged = stage_role(source, root / "staging")
            role = prepare_role(staged)
            self.assertEqual(role.identity, "duo")
            self.assertEqual(role.actions, ("idle", "dance_1", "dance_burst"))
            self.assertIsNone(role.finish_action)
            self.assertTrue((staged / "pet-v2/frames.bin").is_file())
            self.assertFalse((source / "pet-v2").exists())
            (source / "events.json").write_text('{"turn_finished":"dance_1"}')
            staged = stage_role(source, root / "staging-again")
            self.assertEqual(prepare_role(staged).finish_action, "dance_1")

    def test_corrupt_dance_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "duo"
            source.mkdir()
            make_dance(source)
            with (source / "motion.bin").open("r+b") as stream:
                stream.seek(-1, 2)
                stream.write(b"\x00")
            with self.assertRaises(ValueError):
                stage_role(source, root / "staging")

    def test_long_dance_preserves_each_frame_and_its_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "duo"
            source.mkdir()
            make_dance(source, timed_frames=48)
            staged = stage_role(source, root / "staging")
            self.assertEqual(prepare_role(staged).actions, ("idle", "dance_1", "dance_burst"))
            timeline = (staged / "pet-v2/timeline-v3.bin").read_bytes()
            self.assertEqual(struct.unpack_from("<H", timeline, 4)[0], 3)
            corrupt = root / "corrupt"
            shutil.copytree(staged / "pet-v2", corrupt)
            bad = bytearray((corrupt / "timeline-v3.bin").read_bytes())
            struct.pack_into("<H", bad, 20 + 148, 0)
            struct.pack_into("<I", bad, 12, zlib.crc32(bad[20:]))
            (corrupt / "timeline-v3.bin").write_bytes(bad)
            frame_crc = struct.unpack_from("<I", (corrupt / "frames.bin").read_bytes(), 16)[0]
            records = b"".join(struct.pack("<BHBHBBIhh", index, 0, 1, 1000, 1, 0, 1, 2, 2)
                               + struct.pack("<32H", *([0] * 32)) for index in range(3))
            (corrupt / "timeline-v2.bin").write_bytes(
                struct.pack("<4sHHIII", b"PTL1", 2, 3, len(records), zlib.crc32(records), frame_crc) + records)
            with self.assertRaises(ValueError):
                validate_action_pack(corrupt)

    def test_oversized_role_is_rejected_before_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "duo"
            source.mkdir()
            with (source / "too-large.bin").open("wb") as stream:
                stream.truncate(128 * 1024 * 1024 + 1)
            with patch("deskpet_platform.role_staging.shutil.copytree") as copy:
                with self.assertRaises(ValueError):
                    stage_role(source, root / "staging")
                copy.assert_not_called()

    def test_invalid_role_name_is_rejected_before_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad role name"
            source.mkdir()
            with patch("deskpet_platform.role_staging.shutil.copytree") as copy:
                with self.assertRaises(ValueError):
                    stage_role(source, root / "staging")
                copy.assert_not_called()
