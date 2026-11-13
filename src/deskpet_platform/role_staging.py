"""Convert supported source artwork to the common on-card role format."""
from array import array
from pathlib import Path
import shutil
import struct
import sys
import zlib

from PIL import Image
from .card_upload import MAX_FILE_BYTES
from .role_archive import ROLE1_MOTION, extract_role_archive
from .role_install import (
    MAX_ROLE_ACTIONS, MAX_ROLE_BYTES, MAX_ROLE_FRAMES, ROLE_VERSION, is_role1_directory,
    legacy_directory_identity, prepare_role, valid_identity,
)


def _rgba565(value):
    r, g, b = (value >> 11) & 31, (value >> 5) & 63, value & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _decode_frame(data, width, height):
    if len(data) < 512 + height:
        raise ValueError("舞蹈帧不完整。")
    palette = [_rgba565(struct.unpack_from("<H", data, index * 2)[0]) for index in range(256)]
    pixels, offset = bytearray(width * height * 4), 512
    for y in range(height):
        if offset >= len(data):
            raise ValueError("舞蹈帧不完整。")
        runs, offset = data[offset], offset + 1
        previous = 0
        for _ in range(runs):
            if offset + 2 > len(data):
                raise ValueError("舞蹈帧不完整。")
            x, length = data[offset], data[offset + 1]
            offset += 2
            if not length or x < previous or x + length > width or offset + length * 2 > len(data):
                raise ValueError("舞蹈帧的像素范围无效。")
            for column in range(x, x + length):
                color, alpha = data[offset], data[offset + 1]
                offset += 2
                start = (y * width + column) * 4
                pixels[start:start + 4] = bytes((*palette[color], alpha))
            previous = x + length
    if offset != len(data):
        raise ValueError("舞蹈帧长度无效。")
    return Image.frombytes("RGBA", (width, height), bytes(pixels))


def _encode_frame(image):
    indexed = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    mode, palette = indexed.palette.getdata()
    if mode != "RGBA":
        raise ValueError("角色图片的调色板格式无效。")
    palette = palette.ljust(1024, b"\0")
    encoded = bytearray()
    for index in range(256):
        r, g, b, alpha = palette[index * 4:index * 4 + 4]
        color = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        encoded.extend(struct.pack("<HB", color, alpha))
    encoded.extend(indexed.tobytes())
    return bytes(encoded)


def _timing(clip, entries):
    first, count = clip
    durations = [entries[index][3] for index in range(first, first + count)]
    if count > 64 or not 100 <= sum(durations) <= 6000:
        raise ValueError("舞蹈动作帧数或时长超出角色框架限制。")
    return list(range(first, first + count)), durations


def convert_dance(source, destination):
    source = Path(source)
    if source.stat().st_size > 20 + (MAX_ROLE_ACTIONS - 2) * 4 + 128 * 16 + 128 * 65536:
        raise ValueError("舞蹈角色素材过大。")
    data = source.read_bytes()
    try:
        magic, version, width, height, clip_count, frame_count, effect_count, size = struct.unpack_from("<4s6HI", data)
        if (magic != b"DNC1" or version != 1 or not 1 <= width <= 320 or not 1 <= height <= 240 or
                width * height > 65536 - 768 or not 1 <= clip_count <= MAX_ROLE_ACTIONS - 2 or
                not 1 <= effect_count <= frame_count <= 128 or size != len(data)):
            raise ValueError()
        clips = [struct.unpack_from("<HH", data, 20 + index * 4) for index in range(clip_count)]
        table = 20 + clip_count * 4
        entries = [struct.unpack_from("<IIIHH", data, table + index * 16) for index in range(frame_count)]
        expected_frame = 0
        for first, count in clips:
            if not count or first != expected_frame or first + count > frame_count - effect_count:
                raise ValueError()
            expected_frame += count
        if expected_frame != frame_count - effect_count:
            raise ValueError()
        offset = table + frame_count * 16
        for position, length, crc, duration, reserved in entries:
            if (position != offset or length < 512 + height or length > 65536 or
                    position + length > len(data) or not 1 <= duration <= 10000 or reserved or
                    zlib.crc32(data[position:position + length]) != crc):
                raise ValueError()
            offset += length
        if offset != len(data):
            raise ValueError()
    except (struct.error, ValueError) as error:
        raise ValueError("舞蹈角色素材损坏或格式不受支持。") from error

    def frame(index):
        position, length, *_ = entries[index]
        return _decode_frame(data[position:position + length], width, height)

    clips_timed = [_timing(clip, entries) for clip in clips]
    output, cache = [], {}

    def normal(index):
        if index not in cache:
            cache[index] = len(output)
            output.append(_encode_frame(frame(index)))
        return cache[index]

    sequences = [([normal(clips[0][0])], [1000])]
    sequences.extend(([normal(index) for index in indices], durations) for indices, durations in clips_timed)

    # Preserve the separately callable burst by compositing its effect behind
    # the last dance clip. The original effect remains in the source directory.
    burst = []
    base_indices, _ = clips_timed[-1]
    for index in range(24):
        base = frame(base_indices[index * len(base_indices) // 24])
        effect = frame(frame_count - effect_count + (index * effect_count // 24))
        alpha = min(1.0, index / 3, (23 - index) / 3)
        effect.putalpha(effect.getchannel("A").point(lambda value: round(value * alpha)))
        burst.append(len(output))
        output.append(_encode_frame(Image.alpha_composite(effect, base)))
    sequences.append((burst, [(index + 1) * 5000 // 24 - index * 5000 // 24 for index in range(24)]))
    if len(output) > MAX_ROLE_FRAMES:
        raise ValueError("舞蹈角色的帧数超出限制。")

    frame_data = b"".join(output)
    crc = zlib.crc32(frame_data)
    count = len(sequences)
    frame_header = struct.pack("<4sHHHHII", b"PET1", 1, width, height, count, len(output), crc)
    records = bytearray()
    for index, (indices, durations) in enumerate(sequences):
        records.extend(struct.pack("<BHBHBBQhh", index, indices[0], len(indices), sum(durations),
                                   1, 0, 1, width // 2, height - 1))
        records.extend(struct.pack("<64H", *indices, *([0] * (64 - len(indices)))))
        records.extend(struct.pack("<64H", *durations, *([0] * (64 - len(durations)))))
    timeline = struct.pack("<4sHHIII", b"PTL1", 3, count, len(records), zlib.crc32(records), crc) + records
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "frames.bin").write_bytes(frame_header + frame_data)
    (destination / "timeline-v3.bin").write_bytes(timeline)
    return ["idle"] + [f"dance_{index + 1}" for index in range(clip_count)] + ["dance_burst"]


def _convert_background(source, destination):
    source = Path(source)
    if source.stat().st_size not in (512 + 336 * 208, 512 + 336 * 240):
        return
    data = source.read_bytes()
    height = (len(data) - 512) // 336
    palette = struct.unpack_from("<256H", data)
    pixels = array("H")
    for y in range(240):
        row = 512 + min(y, height - 1) * 336 + 8
        pixels.extend(palette[index] for index in data[row:row + 320])
    if sys.byteorder != "little":
        pixels.byteswap()
    Path(destination).write_bytes(pixels.tobytes())


def stage_role(source, work_root):
    source = Path(source).expanduser()
    if source.is_absolute() and not source.is_symlink() and source.is_file():
        target = extract_role_archive(source, work_root)
        _convert_background(target / "land.bin", target / "background.565")
        return target
    if not source.is_absolute() or not source.is_dir() or source.is_symlink():
        raise ValueError("请提供本机角色 ZIP 或角色目录的绝对路径。")
    if not valid_identity(source.name):
        raise ValueError("角色目录名称只支持英文字母、数字、下划线和连字符。")
    count = total = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("角色目录不能包含链接文件或链接目录。")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("角色目录包含无法读取的项目。")
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            raise ValueError("角色文件为空或超过 128 MiB。")
        count += 1
        total += size
        if count > 100 or total > MAX_ROLE_BYTES:
            raise ValueError("角色素材数量或总大小超出限制。")
    portable = is_role1_directory(source)
    target = Path(work_root) / source.name
    if not portable and ROLE_VERSION.fullmatch(source.name):
        identity = legacy_directory_identity(source)
        target = Path(work_root) / "roles" / identity / source.name
    shutil.copytree(source, target, symlinks=True)
    motion = target / "motion.bin"
    if portable:
        names = {entry.name for entry in target.iterdir()}
        if ("motion.bin" in names and not (target / ROLE1_MOTION).exists()
                and motion.is_file() and not motion.is_symlink()):
            motion.rename(target / ROLE1_MOTION)
    elif not (target / "pet-v2/frames.bin").exists():
        if not motion.is_file() or motion.is_symlink():
            raise ValueError("角色目录缺少必需资源 pet-v2/frames.bin 或可转换的 motion.bin。")
        with motion.open("rb") as stream:
            magic = stream.read(4)
        if magic != b"DNC1":
            raise ValueError("角色缺少可识别的动作素材。")
        names = convert_dance(motion, target / "pet-v2")
        (target / "actions.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    if (target / "land.bin").is_file() and not (target / "background.565").exists():
        _convert_background(target / "land.bin", target / "background.565")
    return target


def prepare_role_source(source, work_root):
    """Stage and validate the exact files that will be sent to the board."""
    return prepare_role(stage_role(source, work_root))
