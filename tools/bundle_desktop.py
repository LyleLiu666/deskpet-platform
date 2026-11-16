"""Make a platform installation bundle from a DeskPet sdist and a verified probe archive."""
import argparse
import hashlib
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import zipfile


PROBES = {
    "AgentSpeedProbe-macos-arm64.tar.gz": "macos-arm64",
    "AgentSpeedProbe-windows-x64.zip": "windows-x64",
    "AgentSpeedProbe-linux-x64.tar.gz": "linux-x64",
    "AgentSpeedProbe-linux-arm64.tar.gz": "linux-arm64",
}


def verify(path, checksums):
    with path.open("rb") as stream:
        received = hashlib.file_digest(stream, "sha256").hexdigest()
    if received != checksums.get(path.name):
        raise ValueError(f"SHA-256 mismatch or missing digest for {path.name}")


def extract_probe(archive, destination, binary):
    required = {binary, "SOURCE.txt", "licenses/AgentSpeed-LICENSE.txt", "licenses/zstd-LICENSE.txt"}
    allowed = required | {"licenses/dotnet-LICENSE.txt", "licenses/dotnet-THIRD-PARTY-NOTICES.txt",
                          "licenses/ZstdSharp-LICENSE.txt"}
    destination.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            members = [entry for entry in bundle.infolist() if not entry.is_dir()]
            names = [entry.filename for entry in members]
            if (len(names) != len(set(names)) or not required <= set(names) <= allowed
                    or any(stat.S_ISLNK(entry.external_attr >> 16) for entry in members)):
                raise ValueError("Unexpected files in the probe ZIP")
            bundle.extractall(destination, members)
    else:
        with tarfile.open(archive) as bundle:
            members = [entry for entry in bundle.getmembers() if not entry.isdir()]
            names = [entry.name.removeprefix("./") for entry in members]
            if (len(names) != len(set(names)) or not required <= set(names) <= allowed
                    or any(not entry.isfile() for entry in members)):
                raise ValueError("Unexpected files in the probe tarball")
            bundle.extractall(destination, members, filter="data")
    (destination / binary).chmod(0o755)


def build(source, probe, checksum_file, output):
    checksums = {}
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.removeprefix("*")
        if name in checksums:
            raise ValueError(f"Duplicate digest for {name}")
        checksums[name] = digest
    for path in (source, probe):
        verify(path, checksums)

    target = PROBES[probe.name]
    windows = target.startswith("windows-")
    binary = "AgentSpeedProbe.exe" if windows else "AgentSpeedProbe"
    with tempfile.TemporaryDirectory(prefix="deskpet-desktop-") as temporary:
        tree = Path(temporary)
        with tarfile.open(source) as bundle:
            bundle.extractall(tree, filter="data")
        roots = list(tree.iterdir())
        if len(roots) != 1 or not (roots[0] / "pyproject.toml").is_file():
            raise ValueError("Invalid DeskPet source distribution")
        root = roots[0]
        extract_probe(probe, root / "src/deskpet_platform/assets/token_probes" / target, binary)
        output.mkdir(parents=True, exist_ok=True)
        filename = f"{root.name}-{target}"
        return Path(shutil.make_archive(str(output / filename), "zip" if windows else "gztar",
                                        root_dir=tree, base_dir=root.name))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("probe", type=Path)
    parser.add_argument("checksums", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build(args.source, args.probe, args.checksums, args.output.resolve()))
