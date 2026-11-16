"""Install a desktop archive into a fresh venv and start its discovered probe."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from verify_token_probe import verify as verify_probe


def verify(archive, fixture):
    with tempfile.TemporaryDirectory(prefix="DeskPet clean install ") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        shutil.unpack_archive(archive, source, filter="data" if archive.name.endswith(".tar.gz") else None)
        package, = source.iterdir()
        environment = root / "installed"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        desktop = scripts / ("deskpet-platform.exe" if os.name == "nt" else "deskpet-platform")
        subprocess.run([str(python), "-m", "pip", "install", str(package)], check=True)
        config = root / "fresh-state" / "device.json"
        invocation = [str(desktop), "--config", str(config), "doctor", "--client", "codex"]
        env = dict(os.environ, CODEX_HOME=str(root / "codex"), USERPROFILE=str(root), HOME=str(root),
                   APPDATA=str(root), LOCALAPPDATA=str(root), XDG_CONFIG_HOME=str(root), XDG_DATA_HOME=str(root),
                   DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(root / "runtime"))
        report = json.loads(subprocess.check_output(invocation, cwd=root, env=env, encoding="utf-8"))
        if report["speed"]["status"] != "present":
            raise RuntimeError(f"Bundle probe is not discoverable: {report['speed']}")
        probe = Path(report["speed"]["path"])
        if not probe.is_relative_to(environment) or config.parent.exists():
            raise RuntimeError("Discovery touched device configuration or selected an external probe")
        if json.loads(subprocess.check_output(
                [*invocation, "--no-token-probe"], cwd=root, env=env, encoding="utf-8"))["speed"]["status"] != "disabled":
            raise RuntimeError("Could not disable the bundled probe")
        verify_probe(probe, fixture)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    verify(args.archive.resolve(), args.fixture.resolve())
