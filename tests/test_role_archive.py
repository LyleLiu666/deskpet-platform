from pathlib import Path
import stat
import struct
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
import zlib

from deskpet_platform.role_install import prepare_role
from deskpet_platform.role_sources import list_role_sources
from deskpet_platform.role_staging import prepare_role_source, stage_role
from test_role_pack import make_pack


def make_motion(*, dense=False, glide=False, layered=False, first_gear_frames=2, width=184, height=192):
    pixels = (bytes((1, 0, 160)) + bytes((0, 255)) * 160) * height if dense else (
        bytes((1, 2, 3, 0, 255, 0, 127, 0, 0)) + bytes(height-1))
    counts = (first_gear_frames,) + (1,) * 9 if glide else (1,) * 5
    clips = bytearray()
    first = 0
    for count in counts:
        clips.extend(struct.pack("<HH", first, count))
        first += count
    if glide:
        clips.extend(struct.pack("<HH5Q", 3000, 10000, 2, 1, 1, 1, 1))
    frames = [
        struct.pack("<H", 0x8000 + index) + bytes(510) + pixels
        for index in range(first + 4)
    ]
    if layered:
        role = bytes((1, 2, 3, 0, 255, 0, 127, 0, 0))
        effect = bytes((1, 3, 3, 0, 192, 0, 255, 0, 127))
        fifth_gear = sum(counts[:4])
        frames[fifth_gear] = struct.pack("<H", 0x07e0) + bytes(510) + role * 192
        frames[-1] = struct.pack("<H", 0xf800) + bytes(510) + effect * 192
    offset = 20 + len(clips) + len(frames) * 16
    entries = bytearray()
    for frame in frames:
        entries.extend(struct.pack("<IIIHH", offset, len(frame), zlib.crc32(frame), 300, 0))
        offset += len(frame)
    return (
        struct.pack("<4s6HI", b"DNC1", 3 if glide else 2, width, height, len(counts), len(frames), 1, offset) +
        clips + entries + b"".join(frames)
    )


def make_archive(directory, *, extra=None, link=False, motion=None, width=176, height=160):
    artwork = directory / "pet"
    artwork.mkdir(exist_ok=True)
    make_pack(artwork, width=width, height=height)
    path = directory / "测试角色.zip"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"version":1,"id":"user-archive","name":"测试角色"}')
        archive.writestr("kind", "role1\n")
        row = ZipInfo("row.565")
        row.external_attr = (stat.S_IFLNK | 0o777) << 16 if link else 0o600 << 16
        archive.writestr(row, bytes(320 * 68 * 2))
        archive.writestr("land.bin", bytes(512 + 336 * 240))
        archive.writestr("motion.bin", make_motion() if motion is None else motion)
        for name in ("frames.bin", "timeline-v2.bin"):
            archive.write(artwork / name, "pet-v2/" + name)
        if extra:
            archive.writestr(extra, "ignored")
    return path


class RoleArchiveTests(unittest.TestCase):
    def test_optional_screen_background_is_validated_and_versioned(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=make_archive(root)
            original=prepare_role_source(archive,root/'old')
            with ZipFile(archive,'a') as package:
                package.writestr('background-hd.565',bytes(480*320*2))
            upgraded=prepare_role_source(archive,root/'new')
            self.assertIn('background-hd.565',[item.relative for item in upgraded.files])
            self.assertNotEqual(original.version,upgraded.version)
            self.assertEqual((original.identity,original.actions,original.finish_action),(upgraded.identity,upgraded.actions,upgraded.finish_action))
            self.assertEqual(next(item.source.stat().st_size for item in upgraded.files if item.relative=='background-hd.565'),480*320*2)
            (root/'new'/'user-archive'/'background-hd.565').write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError,'background-hd'):
                prepare_role(root/'new'/'user-archive')

    def test_invalid_screen_background_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=make_archive(root)
            with ZipFile(archive,'a') as package:package.writestr('background-hd.565',b'bad')
            with self.assertRaisesRegex(ValueError,'background-hd'):
                stage_role(archive,root/'rejected')
            self.assertFalse((root/'rejected').exists())

    def test_larger_motion_keeps_the_same_clips_and_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root, motion=make_motion(glide=True, dense=True, width=245, height=256))
            role = prepare_role_source(archive, root / "accepted")
            self.assertEqual(role.actions[-5:], tuple(f"glide_{gear}" for gear in range(1, 6)))

    def test_portable_role_accepts_larger_interaction_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root, width=240, height=218)
            self.assertTrue(prepare_role_source(archive, root / "accepted").portable)

    def test_glide_running_clip_matches_firmware_frame_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root, motion=make_motion(glide=True, first_gear_frames=64))
            self.assertTrue(prepare_role_source(archive, root / "accepted").portable)
            archive = make_archive(root, motion=make_motion(glide=True, first_gear_frames=65))
            with self.assertRaisesRegex(ValueError, "64"):
                prepare_role_source(archive, root / "rejected")

    def test_zip_preserves_motion_and_glide_actions(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            archive = make_archive(work)
            self.assertIn({"id": "user-archive", "path": str(archive), "format": "role1", "name": "测试角色"},
                          list_role_sources(work)["roles"])
            staged = stage_role(archive, work / "stage")
            role = prepare_role(staged)
            self.assertEqual(role.identity, "user-archive")
            self.assertTrue(role.portable)
            self.assertEqual(role.actions[:3], ("idle_breathe", "idle_blink", "wake"))
            self.assertEqual(role.actions[-7:],
                             ("idle", "dance_1", "dance_2", "dance_3", "dance_4", "dance_5", "dance_burst"))
            self.assertEqual((staged / "motion-v2.bin").read_bytes(), make_motion())
            self.assertEqual((staged / "background.565").stat().st_size, 320 * 240 * 2)

            alternate = stage_role(make_archive(work, motion=make_motion(glide=True)), work / "glide")
            glide = prepare_role(alternate)
            self.assertEqual(glide.actions[:-5], role.actions)
            self.assertEqual(glide.actions[-5:], tuple(f"glide_{gear}" for gear in range(1, 6)))

    def test_zip_rejects_extra_and_link_entries_before_staging(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            for fields in ({"extra": "../outside"}, {"link": True}):
                with self.subTest(fields=fields):
                    with self.assertRaises(ValueError):
                        stage_role(make_archive(work, **fields), work / "stage")
                    self.assertFalse((work / "stage").exists())

    def test_zip_rejects_bad_motion_even_when_archive_crc_is_valid(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            bad = bytearray(make_motion())
            bad[-1] ^= 1
            staged = stage_role(make_archive(work, motion=bad), work / "stage")
            with self.assertRaisesRegex(ValueError, "画帧损坏"):
                prepare_role(staged)

    def test_zip_rejects_glide_with_safe_frame_outside_the_speed_clip(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            bad = bytearray(make_motion(glide=True))
            struct.pack_into("<Q", bad, 64, 1 << 2)
            staged = stage_role(make_archive(work, motion=bad), work / "stage")
            with self.assertRaisesRegex(ValueError, "滑行画帧损坏"):
                prepare_role(staged)
