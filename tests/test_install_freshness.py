import os
from email import message_from_bytes
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


class InstallFreshnessTests(unittest.TestCase):
    def test_release_artifacts_include_the_install_entry_and_runtime_assets(self):
        if not shutil.which("uv"):
            self.skipTest("uv is required to build the release artifacts")
        onboarding_files = (
            "docs/flash.md", "docs/role-format.md", "docs/assets/start-qr.png",
            "examples/status.py", "examples/tap.py",
        )
        with tempfile.TemporaryDirectory(prefix="deskpet-package-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for name in ("LICENSE", "MANIFEST.in", "pyproject.toml", "uv.lock", "README.md", "install-macos.command"):
                shutil.copy2(ROOT / name, project / name)
            for name in onboarding_files:
                (project / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, project / name)
            shutil.copytree(ROOT / "src/deskpet_platform", project / "src/deskpet_platform",
                            ignore=shutil.ignore_patterns("__pycache__"))
            subprocess.run(["uv", "build", "--offline"], cwd=project, check=True, capture_output=True, text=True)
            with tarfile.open(next((project / "dist").glob("*.tar.gz"))) as archive:
                members = {item.name.partition("/")[2]: item for item in archive.getmembers()}
                self.assertIn("uv.lock", members)
                self.assertEqual(archive.extractfile(members["LICENSE"]).read(), (ROOT / "LICENSE").read_bytes())
                self.assertTrue(members["install-macos.command"].mode & 0o100)
                for name in (*onboarding_files, "src/deskpet_platform/manuals/operations.md",
                             "src/deskpet_platform/manuals/roles.md"):
                    with self.subTest(archive_file=name):
                        self.assertIn(name, members.keys())
                        self.assertEqual(archive.extractfile(members[name]).read(), (ROOT / name).read_bytes())
            with ZipFile(next((project / "dist").glob("*.whl"))) as archive:
                metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
                project_metadata = message_from_bytes(archive.read(metadata))
                self.assertEqual(project_metadata["License-Expression"], "PolyForm-Noncommercial-1.0.0")
                self.assertIn("LICENSE", project_metadata.get_all("License-File", []))
                license_file = metadata.removesuffix("METADATA") + "licenses/LICENSE"
                self.assertEqual(archive.read(license_file), (ROOT / "LICENSE").read_bytes())
                expected = ["manuals/operations.md", "manuals/development.md", "manuals/roles.md",
                            "role_example.py", "opencode_plugin.js",
                            "assets/agent_icons/LICENSE"]
                expected.extend("assets/agent_icons/" + icon + ".png" for icon in
                                ("antigravity", "chatgpt", "claudecode", "opencode", "workbuddy"))
                for relative in expected:
                    self.assertIn("deskpet_platform/" + relative, archive.namelist())
                self.assertFalse(any(name.startswith(("resources/", "third_party/", "firmware/"))
                                     for name in archive.namelist()))

    def test_non_editable_install_updates_when_package_sources_change(self):
        if not shutil.which("uv"):
            self.skipTest("uv is required for the project installation test")
        with tempfile.TemporaryDirectory(prefix="deskpet-install-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for name in ("LICENSE", "pyproject.toml", "uv.lock", "README.md"):
                shutil.copy2(ROOT / name, project / name)
            shutil.copytree(ROOT / "src/deskpet_platform", project / "src/deskpet_platform",
                            ignore=shutil.ignore_patterns("__pycache__"))
            environment = os.environ.copy()
            environment["UV_PROJECT_ENVIRONMENT"] = str(Path(directory) / "venv")

            def sync():
                subprocess.run(["uv", "sync", "--locked", "--no-editable", "--offline"],
                               cwd=project, env=environment, check=True, capture_output=True, text=True)

            sync()
            probe = project / "src/deskpet_platform/freshness_probe.py"
            probe.write_text("VALUE = 'first'\n", encoding="utf-8")
            sync()
            python = Path(directory) / "venv/bin/python"
            result = subprocess.run([str(python), "-c", "from deskpet_platform.freshness_probe import VALUE; print(VALUE)"],
                                    cwd=project, env=environment, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "first")
            probe.write_text("VALUE = 'second'\n", encoding="utf-8")
            sync()
            result = subprocess.run([str(python), "-c", "from deskpet_platform.freshness_probe import VALUE; print(VALUE)"],
                                    cwd=project, env=environment, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "second")
