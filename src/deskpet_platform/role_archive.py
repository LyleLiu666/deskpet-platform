"""Read the portable role1 ZIP format used by the desktop display."""
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import stat
import struct
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile
import zlib


INTERACTIONS = (
    "idle_breathe", "idle_blink", "wake", "focus", "raise_hand", "show_approval",
    "point_list", "wait_reply", "celebrate_small", "startled", "touched", "disconnected",
    "look_around", "stretch", "nap", "ponder", "walk", "busy", "hurry", "puzzled",
    "wave", "present_message", "celebrate_big", "proud", "deliver_result", "recover",
    "greet", "nod", "thank_you", "goodbye",
)
MOTION_ACTIONS = ("idle", "dance_1", "dance_2", "dance_3", "dance_4", "dance_5", "dance_burst")
GLIDE_ACTIONS = tuple(f"glide_{gear}" for gear in range(1, 6))
ROLE1_MOTION = "motion-v2.bin"
ROLE1_ZIP_FILES = {
    "manifest.json": 4096,
    "kind": 6,
    "row.565": 320 * 68 * 2,
    "land.bin": 512 + 336 * 240,
    "motion.bin": 104 + 128 * (16 + 131072),
    "pet-v2/frames.bin": 20 + 512 * 65536,
    "pet-v2/timeline-v2.bin": 20 + 30 * 80,
}
FIXED_SIZE_FILES = {"kind", "row.565", "land.bin"}
_FRAME = struct.Struct("<IIIHH")


def _valid_cel(data, width, height):
    offset = 512
    visible = False
    for _ in range(height):
        if offset >= len(data):
            return False
        runs = data[offset]
        offset += 1
        if runs > (width + 1) // 2:
            return False
        end = 0
        for _ in range(runs):
            if offset + 2 > len(data):
                return False
            x, count = data[offset:offset + 2]
            offset += 2
            stop = offset + count * 2
            if not count or x < end or x + count > width or stop > len(data):
                return False
            visible |= any(alpha >= 16 for alpha in data[offset + 1:stop:2])
            offset, end = stop, x + count
    return visible and offset == len(data)


def validate_motion(path):
    reason = "角色包的速度、待机、爆气或滑行画帧损坏。"
    try:
        if not 40 <= path.stat().st_size <= ROLE1_ZIP_FILES["motion.bin"]:
            raise ValueError()
        data = path.read_bytes()
        magic, version, width, height, clips, frames, burst, size = struct.unpack_from("<4s6HI", data)
        if (magic != b"DNC1" or (width, height) not in ((184, 192), (245, 256)) or size != len(data) or
                {2: 5, 3: 10}.get(version) != clips or
                not 0 < burst < frames <= 128):
            raise ValueError()
        table = 20 + clips * 4
        segments = tuple(struct.iter_unpack("<HH", data[20:table]))
        moving = 0
        for first, count in segments:
            if first != moving or not count:
                raise ValueError()
            moving += count
        if not 3 <= frames - burst - moving <= 9:
            raise ValueError()
        if version == 3:
            run_ms, glide_ms, *masks = struct.unpack_from("<HH5Q", data, table)
            if not 1 <= run_ms <= 60000 or not 1 <= glide_ms <= 60000:
                raise ValueError()
            if any(count > 64 for _, count in segments[:5]):
                reason = "滑行版角色的每个跑动档位最多包含 64 帧。"
                raise ValueError()
            if any(not mask or mask >> count for mask, (_, count) in zip(masks, segments[:5])):
                raise ValueError()
            table += 44
        offset = table + frames * _FRAME.size
        idle = set()
        for index in range(frames):
            start, length, checksum, duration, reserved = _FRAME.unpack_from(data, table + index * _FRAME.size)
            stop = start + length
            if (start != offset or not 512 + height <= length <= 131072 or stop > len(data) or
                    not 1 <= duration <= 10000 or reserved):
                raise ValueError()
            cel = data[start:stop]
            if zlib.crc32(cel) != checksum or not _valid_cel(cel, width, height):
                raise ValueError()
            if moving <= index < frames - burst:
                if cel in idle:
                    raise ValueError()
                idle.add(cel)
            offset = stop
        if offset != len(data):
            raise ValueError()
        return MOTION_ACTIONS + GLIDE_ACTIONS if version == 3 else MOTION_ACTIONS
    except (OSError, struct.error, ValueError) as error:
        raise ValueError(reason) from error


def validate_resource_names(names, required, *, optional=()):
    counts = Counter(names)
    problems = []
    for label, paths in (
        ("缺少必需资源", required - counts.keys()),
        ("不符合命名规则的资源", counts.keys() - required - set(optional)),
        ("重复资源", (name for name, count in counts.items() if count > 1)),
    ):
        if paths := sorted(paths):
            problems.append(label + "：" + "、".join(repr(path[:150]) for path in paths[:8]) +
                            (f"等 {len(paths)} 项" if len(paths) > 8 else ""))
    if problems:
        raise ValueError("角色资源清单不符。" + "。".join(problems) + "。")


def validate_resource_size(name, size, limit):
    if name in FIXED_SIZE_FILES and size != limit:
        raise ValueError(f"角色资源 {name} 必须为 {limit} 字节。")
    if not 0 < size <= limit:
        raise ValueError(f"角色资源 {name} 不能为空或超过 {limit} 字节。")


def _entries(archive):
    entries = archive.infolist()
    validate_resource_names((entry.orig_filename for entry in entries), ROLE1_ZIP_FILES.keys())
    for entry in entries:
        if (entry.orig_filename != entry.filename or
                stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG) or
                entry.flag_bits & 1 or
                entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
            raise ValueError(f"角色资源 {entry.filename} 必须是未加密的普通文件，使用存储或 Deflate 压缩。")
        validate_resource_size(entry.filename, entry.file_size, ROLE1_ZIP_FILES[entry.filename])
    return entries


def validate_manifest(data):
    try:
        manifest = json.loads(data)
    except (ValueError, RecursionError) as error:
        raise ValueError("角色资源 manifest.json 必须是有效的 JSON。") from error
    if not isinstance(manifest, dict) or manifest.keys() != {"version", "id", "name"}:
        raise ValueError("角色资源 manifest.json 必须包含且仅包含 version、id 和 name。")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("manifest.json 的 version 必须为 1。")
    if not isinstance(manifest["id"], str) or not re.fullmatch(r"user-[a-z0-9][a-z0-9-]{0,42}", manifest["id"]):
        raise ValueError("manifest.json 的 id 必须以 user- 开头，接小写英文字母或数字，可继续使用小写英文字母、数字和连字符，总长最多 48 字符。")
    if not isinstance(manifest["name"], str) or not 0 < len(manifest["name"].strip()) <= 64:
        raise ValueError("manifest.json 的 name 必须为 1 至 64 字符的图鉴显示名。")
    return manifest


def inspect_role_archive(source):
    try:
        if source.stat().st_size > sum(ROLE1_ZIP_FILES.values()) + 65536:
            return None
        with ZipFile(source) as archive:
            _entries(archive)
            return validate_manifest(archive.read("manifest.json")) if archive.read("kind") == b"role1\n" else None
    except (BadZipFile, NotImplementedError, OSError, ValueError, zlib.error):
        return None


def extract_role_archive(source, work_root):
    """Extract only known regular files, under an identity from the manifest."""
    if source.stat().st_size > sum(ROLE1_ZIP_FILES.values()) + 65536:
        raise ValueError("角色 ZIP 过大。")
    try:
        with ZipFile(source) as archive:
            entries = _entries(archive)
            manifest = validate_manifest(archive.read("manifest.json"))
            if archive.read("kind") != b"role1\n":
                raise ValueError("角色资源 kind 必须是 role1 加一个换行。")
            target = Path(work_root) / manifest["id"]
            (target / "pet-v2").mkdir(parents=True)
            for entry in entries:
                name = ROLE1_MOTION if entry.filename == "motion.bin" else entry.filename
                with archive.open(entry) as contents, (target / name).open("xb") as output:
                    shutil.copyfileobj(contents, output)
    except (BadZipFile, NotImplementedError, UnicodeError, OSError, zlib.error) as error:
        raise ValueError("角色 ZIP 无法读取。") from error
    return target
