"""Role feature requirements shared by catalog selection and install preflight."""
from dataclasses import dataclass
from pathlib import Path
import struct
from zipfile import ZipFile

from .role_archive import ROLE1_BURST_AUDIO, ROLE1_MOTION
from .role_install import RoleBundle, is_role1_directory

VARIANTS = ("lite", "hd", "photo", "video", "video-burst")


@dataclass(frozen=True)
class RoleRequirements:
    variant: str
    requires: tuple[str, ...]
    requires_legacy_compatible: bool = False

    @property
    def supported_boards(self):
        if self.variant == "lite":
            return ("es3c35p",) if self.requires_legacy_compatible else ("esp32-2432s028", "es3c35p")
        return ("es3c35p",)

    def missing(self, capabilities):
        missing = [flag for flag in self.requires if capabilities.get(flag) != 1]
        if self.requires_legacy_compatible and capabilities.get(
            "role1_required", capabilities.get("pet_view") is True
        ) == 1:
            missing.append("role1_required")
        return missing


def _inspect_files(names, read):
    features, variant = ["role_zip_motion"], "lite"
    frames = read("pet-v2/frames.bin", 10)
    timeline = read("pet-v2/timeline-v2.bin", 20 + 30 * 80 + 1)
    if len(timeline) < 20 or (len(timeline) - 20) % 80:
        raise ValueError("角色帧时间轴长度无效。")
    anchors = (struct.unpack_from("<hh", timeline, offset + 12)
               for offset in range(20, len(timeline), 80))
    if frames[6:10] != struct.pack("<HH", 176, 160) or any(a != (88, 159) for a in anchors):
        features.append("role_large_frames")
        variant = "hd"

    motion_name = ROLE1_MOTION if ROLE1_MOTION in names else "motion.bin"
    if read(motion_name, 10)[6:10] != struct.pack("<HH", 184, 192):
        features.append("role_large_motion")
        variant = "hd"
    if "background-hd.565" in names:
        features.append("role_hd_background")
        variant = "hd"
    if "scene-frames.bin" in names:
        magic = read("scene-frames.bin", 4)
        if magic not in (b"RSC1", b"RSC2"):
            raise ValueError("角色的原屏照片帧格式无效。")
        features.append("role_photo_motion")
        variant = "photo"
        if magic == b"RSC2":
            features.append("role_video_timeline")
            variant = "video"
    if ROLE1_BURST_AUDIO in names:
        if variant != "video":
            raise ValueError("爆发舞步的音乐需要视频角色。")
        features.append("role_burst_audio")
        variant = "video-burst"
    return RoleRequirements(variant, tuple(features))


def inspect_role_requirements(source):
    """Inspect bounded headers. The normal installer still validates the complete bundle."""
    if isinstance(source, RoleBundle):
        if not source.portable:
            return RoleRequirements("lite", (), True)
        files = {item.relative: item.source for item in source.files}
    else:
        path = Path(source)
        if path.is_file() and path.suffix.lower() == ".zip":
            with ZipFile(path) as archive:
                def read(name, size):
                    with archive.open(name) as stream:
                        return stream.read(size)
                return _inspect_files(set(archive.namelist()), read)
        if not is_role1_directory(path):
            return RoleRequirements("lite", (), True)
        if (path / "pet-v2").is_symlink():
            raise ValueError("角色帧目录不能是链接。")
        files = {name: path / name for name in (
            "pet-v2/frames.bin", "pet-v2/timeline-v2.bin", ROLE1_MOTION, "motion.bin",
            "background-hd.565", "scene-frames.bin", ROLE1_BURST_AUDIO,
        ) if not (path / name).is_symlink() and (path / name).is_file()}

    def read(name, size):
        with files[name].open("rb") as stream:
            return stream.read(size)

    return _inspect_files(files, read)
