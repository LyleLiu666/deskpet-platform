import os
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
        with tempfile.TemporaryDirectory(prefix="deskpet-package-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for name in ("LICENSE", "MANIFEST.in", "pyproject.toml", "uv.lock", "README.md", "install-macos.command"):
                shutil.copy2(ROOT / name, project / name)
            shutil.copytree(ROOT / "src/deskpet_platform", project / "src/deskpet_platform",
                            ignore=shutil.ignore_patterns("__pycache__"))
            subprocess.run(["uv", "build", "--offline"], cwd=project, check=True, capture_output=True, text=True)
            with tarfile.open(next((project / "dist").glob("*.tar.gz"))) as archive:
                members = {item.name.partition("/")[2]: item for item in archive.getmembers()}
                self.assertIn("LICENSE", members)
                self.assertIn("uv.lock", members)
                self.assertTrue(members["install-macos.command"].mode & 0o100)
            with ZipFile(next((project / "dist").glob("*.whl"))) as archive:
                self.assertTrue(any(name.endswith(".dist-info/licenses/LICENSE") for name in archive.namelist()))
                expected = ["manuals/operations.md", "manuals/development.md", "manuals/roles.md",
                            "role_example.py", "opencode_plugin.js",
                            "assets/agent_icons/LICENSE"]
                expected.extend("assets/agent_icons/" + icon + ".png" for icon in
                                ("antigravity", "claudecode", "codex", "opencode", "workbuddy"))
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
            for name in ("pyproject.toml", "uv.lock", "README.md"):
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
