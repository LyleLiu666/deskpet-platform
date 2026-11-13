"""Validate an on-disk role before transferring a complete version to a card."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

from .card_upload import MAX_FILE_BYTES, validate_path
from .role_archive import (
    INTERACTIONS, ROLE1_MOTION, ROLE1_ZIP_FILES, validate_manifest, validate_motion, validate_resource_names,
    validate_resource_size,
)

MAX_ROLE_BYTES = 512 * 1024 * 1024
MAX_ROLE_ACTIONS = 48
MAX_ROLE_FRAMES = 1024
MAX_ROLE_ID_LENGTH = 48
ROLE_METADATA_FILES = ("actions.txt", "events.json", "event.txt")
ROLE_VERSION = re.compile(r"r-[0-9a-f]{16}")


def valid_identity(value):
    return bool(re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_ROLE_ID_LENGTH}}}", value))


@dataclass(frozen=True)
class RoleFile:
    relative: str
    source: Path
    sha256: str


@dataclass(frozen=True)
class RoleBundle:
    identity: str
    version: str
    files: tuple[RoleFile, ...]
    actions: tuple[str, ...]
    finish_action: str | None
    portable: bool = False


def check_unique(relative, seen):
    name = relative.casefold()
    if name in seen:
        raise ValueError(f"角色资源路径 {relative!r} 在储存卡上会发生大小写冲突。")
    seen.add(name)


def _declares_role1(root):
    kind = root / "kind"
    if not kind.is_file() or kind.is_symlink():
        return False
    with kind.open("rb") as stream:
        return stream.read(7) == b"role1\n"


def is_role1_directory(root):
    if (root / ROLE1_MOTION).exists() or _declares_role1(root):
        return True
    manifest = root / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        with manifest.open("rb") as stream:
            limit = ROLE1_ZIP_FILES[manifest.name]
            data = stream.read(limit + 1)
        validate_resource_size(manifest.name, len(data), limit)
        validate_manifest(data)
    except ValueError:
        return False
    return True


def legacy_directory_identity(root):
    identity = root.name
    if ROLE_VERSION.fullmatch(identity):
        if root.parent.parent.name != "roles":
            raise ValueError("无法确认备份的角色 ID，请保留 roles/<角色 ID>/<版本> 目录结构。")
        identity = root.parent.name
    if not valid_identity(identity):
        raise ValueError("角色目录名称只支持英文字母、数字、下划线和连字符。")
    return identity


def role1_directory_identity(root):
    manifest = root / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("role1 角色目录缺少必需资源 manifest.json。")
    validate_resource_size(manifest.name, manifest.stat().st_size, ROLE1_ZIP_FILES[manifest.name])
    identity = validate_manifest(manifest.read_bytes())["id"]
    if root.name != identity and not ROLE_VERSION.fullmatch(root.name):
        raise ValueError(f"角色目录名须为 manifest.json 中的 id {identity!r} 或储存卡上的版本名 r-<16 位小写十六进制>。")
    return identity


def validate_role1_directory(root, filenames):
    expected = dict(ROLE1_ZIP_FILES)
    expected[ROLE1_MOTION] = expected.pop("motion.bin")
    validate_resource_names(filenames, expected.keys(), optional=(*ROLE_METADATA_FILES, "background.565"))
    for name, limit in expected.items():
        validate_resource_size(name, (root / name).stat().st_size, limit)
    if not _declares_role1(root):
        raise ValueError("角色资源 kind 必须是 role1 加一个换行。")
    return role1_directory_identity(root)


def _read_finish_action(root, filenames, actions, default):
    if "events.json" in filenames:
        try:
            events = root / "events.json"
            if events.stat().st_size > 4096:
                raise ValueError()
            data = json.loads(events.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) - {"turn_finished"}:
                raise ValueError()
            finish = data.get("turn_finished")
            if finish is not None and (not isinstance(finish, str) or finish not in actions):
                raise ValueError()
            return finish
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError("角色资源 events.json 无效，turn_finished 必须为空或指向已有动作。") from error
    if "event.txt" in filenames:
        try:
            with (root / "event.txt").open("rb") as stream:
                data = stream.read(34)
            if not 1 <= len(data) <= 33 or not data.endswith(b"\n"):
                raise ValueError()
            finish = data[:-1].decode("ascii")
            if finish in ("", "-"):
                return None
            if finish not in actions:
                raise ValueError()
            return finish
        except (OSError, ValueError) as error:
            raise ValueError("角色资源 event.txt 无效，必须是一行已有动作名或 -。") from error
    return default


def validate_action_pack(directory, *, portable=False):
    frames = directory / "frames.bin"
    timeline = directory / "timeline-v3.bin"
    if not timeline.exists():
        timeline = directory / "timeline-v2.bin"
    try:
        with frames.open("rb") as stream:
            header = stream.read(20)
            magic, version, width, height, actions, count, expected = struct.unpack("<4sHHHHII", header)
            if (magic != b"PET1" or version != 1 or not 1 <= width <= 320 or not 1 <= height <= 240 or
                    width * height > 65536 - 768 or not 1 <= actions <= MAX_ROLE_ACTIONS or not 1 <= count <= MAX_ROLE_FRAMES or
                    frames.stat().st_size != 20 + count * (768 + width * height)):
                raise ValueError()
            if portable and ((width, height) != (176, 160) or actions not in (12, 30) or count > 512):
                raise ValueError()
            crc = 0
            while chunk := stream.read(256 * 1024):
                crc = zlib.crc32(chunk, crc)
            if crc != expected:
                raise ValueError()
        if timeline.stat().st_size > 20 + MAX_ROLE_ACTIONS * 276:
            raise ValueError()
        data = timeline.read_bytes()
        magic, version, entries, length, checksum, frame_crc = struct.unpack("<4sHHIII", data[:20])
        stride = {2: 80, 3: 276}.get(version, 0)
        if (magic != b"PTL1" or not stride or entries != actions or length != entries * stride or
                len(data) != 20 + length or frame_crc != expected or zlib.crc32(data[20:]) != checksum):
            raise ValueError()
        if portable and version != 2:
            raise ValueError()
        timed = version == 3
        capacity = 64 if timed else 32
        record = struct.Struct("<BHBHBBQhh" if timed else "<BHBHBBIhh")
        indices = struct.Struct(f"<{capacity}H")
        for index in range(entries):
            offset = 20 + index * stride
            identity, first, number, duration, loop, fallback, safe, x, y = record.unpack_from(data, offset)
            if (identity != index or not 1 <= number <= capacity or first >= count or not 100 <= duration <= 6000 or
                    loop > 1 or fallback >= entries or not safe or safe >> number or
                    not 0 <= x < width or not 0 <= y < height):
                raise ValueError()
            if portable and (x, y) != (88, 159):
                raise ValueError()
            order_start = offset + record.size
            if any(frame >= count for frame in indices.unpack_from(data, order_start)[:number]):
                raise ValueError()
            if timed:
                intervals = indices.unpack_from(data, order_start + indices.size)[:number]
                if not all(intervals) or sum(intervals) != duration:
                    raise ValueError()
        return actions
    except (OSError, struct.error, ValueError) as error:
        raise ValueError(f"角色互动资源 pet-v2/frames.bin 或 pet-v2/{timeline.name} 损坏或格式不符。") from error


def prepare_role(source):
    root = Path(source).expanduser()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("角色目录必须是本机存在的绝对路径。")
    paths = sorted(root.rglob("*"))
    filenames = set()
    seen = set()
    for path in paths:
        if path.is_symlink():
            raise ValueError("角色目录不能包含链接文件或链接目录。")
        relative = path.relative_to(root).as_posix()
        check_unique(relative, seen)
        if path.is_file():
            filenames.add(relative)
    portable = is_role1_directory(root)
    if portable:
        identity = validate_role1_directory(root, filenames)
        motion_actions = validate_motion(root / ROLE1_MOTION)
    else:
        identity = legacy_directory_identity(root)
        motion_actions = ()
        if "pet-v2/frames.bin" not in filenames:
            raise ValueError("角色目录缺少必需资源 pet-v2/frames.bin。")
        if not filenames.intersection(("pet-v2/timeline-v2.bin", "pet-v2/timeline-v3.bin")):
            raise ValueError("角色目录缺少必需资源 pet-v2/timeline-v2.bin 或 pet-v2/timeline-v3.bin。")
    if "background.565" in filenames and (root / "background.565").stat().st_size != 320 * 240 * 2:
        raise ValueError("角色资源 background.565 必须为 153600 字节。")
    action_count = validate_action_pack(root / "pet-v2", portable=portable)
    expected_actions = INTERACTIONS[:action_count] + motion_actions if portable else None
    action_count += len(motion_actions)
    action_file = root / "actions.txt"
    manifest = root / "pet-v2/manifest.json"
    if "actions.txt" not in filenames and expected_actions is None and "pet-v2/manifest.json" not in filenames:
        raise ValueError("角色目录缺少动作名称资源 actions.txt 或 pet-v2/manifest.json。")
    try:
        if "actions.txt" in filenames:
            if action_file.stat().st_size > 4096:
                raise ValueError()
            action_names = action_file.read_text(encoding="utf-8").splitlines()
        elif expected_actions is not None:
            action_names = list(expected_actions)
        else:
            if manifest.stat().st_size > 1024 * 1024:
                raise ValueError()
            data = json.loads(manifest.read_text(encoding="utf-8"))
            action_names = [entry["id"] for entry in data["actions"]]
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise ValueError("无法从 actions.txt 或 pet-v2/manifest.json 读取动作名称。") from error
    for index, name in enumerate(action_names, start=1):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValueError(f"第 {index} 个动作名称无效。名称必须以小写字母开头，使用小写字母、数字或下划线，最长 32 字符。")
    if len(action_names) != action_count:
        raise ValueError(f"角色需要 {action_count} 个动作名称，实际提供 {len(action_names)} 个。")
    if len(set(action_names)) != action_count or sum(len(name) + 1 for name in action_names) > 600:
        raise ValueError("角色动作名称必须唯一，名称与换行合计最多 600 字节。")
    if expected_actions is not None:
        for index, (actual, expected) in enumerate(zip(action_names, expected_actions), start=1):
            if actual != expected:
                raise ValueError(f"第 {index} 个 role1 动作名称应为 {expected!r}，实际为 {actual!r}。")
    default_finish = None
    if portable and not ROLE_VERSION.fullmatch(root.name):
        default_finish = "deliver_result" if "deliver_result" in action_names else "celebrate_small"
    finish_action = _read_finish_action(root, filenames, action_names, default_finish)
    sources, total_size = [], 0
    for path in paths:
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("角色目录包含无法读取的项目。")
        relative = path.relative_to(root).as_posix()
        if relative in ROLE_METADATA_FILES:
            continue
        try:
            validate_path(f"roles/{identity}/r-0000000000000000/{relative}")
        except ValueError as error:
            raise ValueError(f"角色资源路径 {relative!r} 不符合储存卡命名规则。") from error
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            raise ValueError("角色文件为空或超过 128 MiB。")
        total_size += size
        if total_size > MAX_ROLE_BYTES:
            raise ValueError("角色素材总大小超过 512 MiB。")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(256 * 1024):
                digest.update(chunk)
        sources.append(RoleFile(relative, path, digest.hexdigest()))
        if len(sources) > 100:
            raise ValueError("一个角色最多包含 100 个文件。")
    version_hash = hashlib.sha256()
    for item in sources:
        version_hash.update(item.relative.encode("ascii") + b"\0" + bytes.fromhex(item.sha256))
    version_hash.update(b"\0actions\0" + "\n".join(action_names).encode("ascii"))
    version_hash.update(b"\0turn_finished\0" + (finish_action or "").encode("ascii"))
    return RoleBundle(identity, "r-" + version_hash.hexdigest()[:16], tuple(sources), tuple(action_names), finish_action,
                      portable)
