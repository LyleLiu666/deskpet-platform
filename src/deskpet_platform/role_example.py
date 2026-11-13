"""Build the Pocket Bot example without external artwork or a connected board."""
from io import BytesIO
import json
from pathlib import Path
import struct
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

from PIL import Image, ImageDraw, ImageFont

from deskpet_platform.image_display import _rgb565
from deskpet_platform.role_archive import INTERACTIONS
from deskpet_platform.role_staging import _encode_frame, prepare_role_source


IDENTITY = "user-pocket-bot"
NAME = "Pocket Bot"
MOTION_SIZE = (184, 192)
INTERACTION_BOX = (4, 25, 180, 185)


def pose(*, lift=0, stride=0, blink=False, wave=False, smile=False):
    image = Image.new("RGBA", MOTION_SIZE)
    draw = ImageDraw.Draw(image)
    ink, teal, light = "#163841", "#42d4b3", "#b9ffdc"
    draw.rounded_rectangle((58 - stride, 147 - lift, 81 - stride, 165 - lift), 7, fill=ink)
    draw.rounded_rectangle((102 + stride, 147 - lift, 125 + stride, 165 - lift), 7, fill=ink)
    draw.rounded_rectangle((45, 114 - lift, 64, 143 - lift), 7, fill=teal)
    if wave:
        draw.line((121, 134 - lift, 145, 116 - lift), fill=teal, width=13)
        draw.rounded_rectangle((138, 89 - lift, 156, 123 - lift), 7, fill=teal)
    else:
        draw.rounded_rectangle((118, 114 - lift, 138, 143 - lift), 7, fill=teal)
    draw.rounded_rectangle((59, 106 - lift, 124, 154 - lift), 12, fill=ink)
    draw.rounded_rectangle((69, 118 - lift, 114, 143 - lift), 7, fill=teal)
    draw.ellipse((86, 126 - lift, 97, 137 - lift), fill=light)
    draw.line((91, 70 - lift, 91, 57 - lift), fill=ink, width=4)
    draw.ellipse((86, 48 - lift, 96, 58 - lift), fill="#ffc967")
    draw.rounded_rectangle((47, 68 - lift, 135, 111 - lift), 14, fill=teal)
    draw.rounded_rectangle((55, 76 - lift, 127, 103 - lift), 10, fill=ink)
    for x in (74, 106):
        draw.rounded_rectangle((x, 85 - lift, x + 6, (87 if blink else 94) - lift), 2, fill=light)
    if smile:
        draw.arc((84, 85 - lift, 99, 99 - lift), 10, 170, fill=light, width=2)
    return image


def burst_effect():
    image = Image.new("RGBA", MOTION_SIZE)
    draw = ImageDraw.Draw(image)
    for x, y, radius in ((37, 65, 10), (150, 61, 13), (155, 121, 8), (37, 120, 6)):
        draw.polygon([(x, y - radius), (x + 3, y - 3), (x + radius, y), (x + 3, y + 3),
                      (x, y + radius), (x - 3, y + 3), (x - radius, y), (x - 3, y - 3)],
                     fill="#ffc967")
    return image


def scenery():
    stage = Image.new("RGB", (336, 240), "#eff5e8")
    draw = ImageDraw.Draw(stage)
    draw.rectangle((0, 173, 335, 239), fill="#c3ded1")
    draw.ellipse((55, 153, 145, 180), fill="#a4c8b8")
    return stage


def catalog_row(avatar):
    row = Image.new("RGB", (320, 68), "#eff5e8")
    icon = avatar.crop(avatar.getbbox())
    icon.thumbnail((54, 54), Image.Resampling.LANCZOS)
    row.paste(icon, (14 + (54 - icon.width) // 2, (68 - icon.height) // 2), icon)
    ImageDraw.Draw(row).text((84, 34), NAME, anchor="lm", fill="#163841",
                            font=ImageFont.load_default(size=20))
    return _rgb565(row)


def indexed_palette(image):
    indexed = image.quantize(colors=256)
    colors = bytes(indexed.getpalette()).ljust(768, b"\0")
    return indexed, _rgb565(Image.frombytes("RGB", (256, 1), colors))


def motion_cel(image):
    indexed, palette = indexed_palette(image.convert("RGB"))
    data = bytearray(palette)
    indices, alphas = indexed.tobytes(), image.getchannel("A").tobytes()
    for row in range(0, len(alphas), image.width):
        row_alpha = alphas[row:row + image.width]
        # Each record describes a contiguous run of visible pixels in one row.
        visible = bytes(alpha >= 16 for alpha in row_alpha)
        edges = [x for x, (left, right) in enumerate(zip(b"\0" + visible, visible + b"\0")) if left != right]
        data.append(len(edges) // 2)
        for start, end in zip(edges[::2], edges[1::2]):
            data.extend((start, end - start))
            for x in range(start, end):
                data.extend((indices[row + x], row_alpha[x]))
    return bytes(data)


def motion_file(gears, idle, burst):
    segments = bytearray()
    frames = []
    for gear in gears:
        segments.extend(struct.pack("<HH", len(frames), len(gear)))
        frames.extend(gear)
    frames.extend(idle)
    frames.extend(burst)
    offset = 20 + len(segments) + 16 * len(frames)
    table, payload = bytearray(), bytearray()
    for frame, milliseconds in frames:
        table.extend(struct.pack("<IIIHH", offset + len(payload), len(frame), zlib.crc32(frame), milliseconds, 0))
        payload.extend(frame)
    header = struct.pack("<4s6HI", b"DNC1", 2, *MOTION_SIZE, len(gears), len(frames), len(burst), offset + len(payload))
    return header + segments + table + payload


def interaction_files(poses, clips):
    frames = b"".join(_encode_frame(image.crop(INTERACTION_BOX)) for image in poses)
    checksum = zlib.crc32(frames)
    actions = INTERACTIONS[:12]
    records = bytearray()
    for index, name in enumerate(actions):
        order, duration, loop = clips[name]
        safe = 1 | (1 << (len(order) - 1))
        records.extend(struct.pack("<BHBHBBIhh", index, order[0], len(order), duration, loop, 0, safe, 88, 159))
        records.extend(struct.pack("<32H", *order, *([0] * (32 - len(order)))))
    width = INTERACTION_BOX[2] - INTERACTION_BOX[0]
    height = INTERACTION_BOX[3] - INTERACTION_BOX[1]
    header = struct.pack("<4sHHHHII", b"PET1", 1, width, height, len(actions), len(poses), checksum)
    timeline = struct.pack("<4sHHIII", b"PTL1", 2, len(actions), len(records), zlib.crc32(records), checksum)
    return header + frames, timeline + records


def example_files():
    standing, blink, happy, wave = pose(), pose(blink=True), pose(smile=True), pose(wave=True)
    idle = [(motion_cel(image), ms) for image, ms in ((standing, 1350), (blink, 140), (happy, 550))]
    gears = []
    for gear, ms in enumerate((280, 225, 180, 145, 110), start=1):
        gears.append([(motion_cel(pose(lift=gear + 3, stride=-gear)), ms),
                      (motion_cel(pose(stride=gear, smile=True)), ms)])
    motion = motion_file(gears, idle, [(motion_cel(burst_effect()), 500)])
    clips = {
        "idle_breathe": ((0, 2, 0), 1500, True),
        "idle_blink": ((0, 1, 0), 420, False),
        "wake": ((1, 2, 0), 720, False),
        "focus": ((0,), 900, True),
        "raise_hand": ((0, 3, 3, 0), 1200, False),
        "show_approval": ((0, 2, 2, 0), 1000, False),
        "point_list": ((0, 3, 0), 900, False),
        "wait_reply": ((0, 1, 0), 1800, True),
        "celebrate_small": ((0, 2, 3, 2, 0), 1300, False),
        "startled": ((2, 1, 0), 600, False),
        "touched": ((0, 3, 2, 0), 960, False),
        "disconnected": ((0, 1, 1), 1500, True),
    }
    frames, timeline = interaction_files((standing, blink, happy, wave), clips)
    background, palette = indexed_palette(scenery())
    return {
        "manifest.json": json.dumps({"version": 1, "id": IDENTITY, "name": NAME}, ensure_ascii=False),
        "kind": b"role1\n",
        "row.565": catalog_row(standing),
        "land.bin": palette + background.tobytes(),
        "motion.bin": motion,
        "pet-v2/frames.bin": frames,
        "pet-v2/timeline-v2.bin": timeline,
    }


def create_role_example(destination):
    path = Path(destination).expanduser()
    if not path.is_absolute() or path.suffix.lower() != ".zip" or not path.parent.is_dir():
        raise ValueError("请提供位于已有目录中的新 ZIP 文件绝对路径。")
    package = BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in example_files().items():
            archive.writestr(name, data)
    data = package.getvalue()
    with tempfile.TemporaryDirectory(prefix="deskpet-role-example-") as directory:
        sample = Path(directory) / "example.zip"
        sample.write_bytes(data)
        prepare_role_source(sample, directory)
    try:
        output = path.open("xb")
    except FileExistsError:
        raise ValueError("目标文件已存在，请选择新的 ZIP 文件名。") from None
    try:
        with output:
            output.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return {"path": str(path), "id": IDENTITY, "name": NAME, "format": "role1"}
