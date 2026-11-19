"""Discover local role candidates without coupling the platform to any role."""
from pathlib import Path
from zipfile import BadZipFile

from .role_requirements import VARIANTS, inspect_role_requirements

from .role_archive import inspect_role_archive
from .role_install import is_role1_directory, legacy_directory_identity, role1_directory_identity, valid_identity


def list_role_sources(directory, capabilities=None):
    if directory is None:
        return {
            "status": "not_configured", "roles": [], "catalog": [],
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
    grouped = {}
    for role in roles:
        try:
            requirements = inspect_role_requirements(role["path"])
        except (BadZipFile, KeyError, OSError, ValueError):
            continue
        entry = grouped.setdefault(role["id"], {"id": role["id"], "name": role.get("name", role["id"]),
                                               "variants": []})
        missing = requirements.missing(capabilities) if capabilities is not None else []
        if capabilities is not None:
            missing[:0] = [flag for flag in ("card_transfer", "role_actions") if capabilities.get(flag) != 1]
        entry["variants"].append({
            "path": role["path"], "variant": requirements.variant, "requires": list(requirements.requires),
            "compatible": not missing if capabilities is not None else None,
            "missing_capabilities": missing,
        })
    for entry in grouped.values():
        candidates = [choice for choice in entry["variants"] if choice["compatible"]]
        winners = preferred_variants(candidates)
        entry["installable"] = len(winners) == 1 if capabilities is not None else None
        entry["selected_variant"] = winners[0]["variant"] if len(winners) == 1 else None
    return {"status": "ok", "directory": str(root), "roles": roles, "catalog": list(grouped.values())}


def preferred_variants(choices):
    if not choices:
        return []
    best = max(VARIANTS.index(choice["variant"]) for choice in choices)
    return [choice for choice in choices if VARIANTS.index(choice["variant"]) == best]


def select_role_source(directory, identity, capabilities, variant=None):
    catalog = list_role_sources(directory, capabilities)["catalog"]
    entry = next((item for item in catalog if item["id"] == identity), None)
    if entry is None:
        return {"status": "unsupported", "reason": "role_source_not_found", "role_id": identity}
    choices = [choice for choice in entry["variants"] if variant is None or choice["variant"] == variant]
    winners = preferred_variants([choice for choice in choices if choice["compatible"]])
    if len(winners) == 1:
        return {"status": "ok", "path": winners[0]["path"]}
    if winners:
        return {"status": "invalid", "reason": "conflicting_role_variants", "role_id": identity,
                "available_variants": winners}
    missing = sorted({flag for choice in choices for flag in choice["missing_capabilities"]})
    return {"status": "unsupported", "reason": "no_compatible_variant", "role_id": identity,
            "missing_capabilities": missing, "available_variants": entry["variants"]}
