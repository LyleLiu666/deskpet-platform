import struct
from pathlib import Path
import zlib


ROOT = Path(__file__).resolve().parents[1]


def make_pack(directory, *, width=176, height=160, actions=12, frame_count=None):
    palette = b"\x00\xf8\xff" + bytes(255 * 3)
    frame = palette + bytes(width * height)
    frame_count = frame_count or actions
    frames = frame * frame_count
    checksum = zlib.crc32(frames)
    (directory / "frames.bin").write_bytes(
        struct.pack("<4sHHHHII", b"PET1", 1, width, height, actions, frame_count, checksum) + frames)
    records = b"".join(
        struct.pack("<BHBHBBIhh", action, action, 1, 1000, 1, 0, 1, min(88, width-1), min(159, height-1))
        + struct.pack("<32H", action, *([0] * 31)) for action in range(actions))
    (directory / "timeline-v2.bin").write_bytes(
        struct.pack("<4sHHIII", b"PTL1", 2, actions, len(records), zlib.crc32(records), checksum) + records)


def write_role_actions(directory):
    names = ["idle", "wake"] + [f"action_{index}" for index in range(10)]
    (directory / "actions.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
