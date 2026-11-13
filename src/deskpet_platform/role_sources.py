"""Discover local role candidates without coupling the platform to any role."""
from pathlib import Path

from .role_archive import inspect_role_archive
from .role_install import is_role1_directory, legacy_directory_identity, role1_directory_identity, valid_identity


def list_role_sources(directory):
    if directory is None:
        return {
            "status": "not_configured", "roles": [],
            "next_step": "启动 MCP 时设置 --role-dir，或提供角色素材目录的绝对路径。",
        }
    root = Path(directory).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("角色素材目录必须是本机存在的绝对路径。")

    def begins(path, magic):
        if path.is_symlink() or not path.is_file():
            return False
        try:
            with path.open("rb") as stream:
                return stream.read(len(magic)) == magic
        except OSError:
            return False

    roles = []
    for source in sorted(root.iterdir()):
        if source.is_symlink():
            continue
        if source.suffix.lower() == ".zip" and source.is_file():
            manifest = inspect_role_archive(source)
            if manifest is not None:
                roles.append({"id": manifest["id"], "path": str(source), "format": "role1", "name": manifest["name"]})
            continue
        if not source.is_dir() or not valid_identity(source.name):
            continue
        try:
            if is_role1_directory(source):
                roles.append({"id": role1_directory_identity(source), "path": str(source), "format": "role1"})
                continue
            identity = legacy_directory_identity(source)
        except (OSError, ValueError):
            continue
        pack = source / "pet-v2"
        if (not pack.is_symlink() and begins(pack / "frames.bin", b"PET1") and
                (begins(pack / "timeline-v3.bin", b"PTL1") or begins(pack / "timeline-v2.bin", b"PTL1"))):
            kind = "PET1"
        elif begins(source / "motion.bin", b"DNC1"):
            kind = "DNC1"
        else:
            continue
        roles.append({"id": identity, "path": str(source), "format": kind})
    return {"status": "ok", "directory": str(root), "roles": roles}
