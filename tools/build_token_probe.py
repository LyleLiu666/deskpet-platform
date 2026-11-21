"""Build and smoke-test an AgentSpeedProbe archive on its target OS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import urlretrieve

from verify_token_probe import verify


AGENT_SPEED = "7b2d448ffff24343d9f70b66e8c23b88dc1d16f7"  # v1.8.0
ZSTD_SHARP = "2cd0c019693bc786a5fe5c3be94e107b24e7267e"
TARGETS = {
    ("Darwin", "arm64"): "macos-arm64",
    ("Windows", "amd64"): "win-x64",
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
}
FIXTURE = "spec/fixtures/codex/sessions/2026/10/20/rollout-2026-10-20T10-00-00-modern.jsonl"


def fetch(work, repo, revision, sha256):
    archive = work / f"{revision}.tar.gz"
    urlretrieve(f"https://codeload.github.com/{repo}/tar.gz/{revision}", archive)
    with archive.open("rb") as content:
        if hashlib.file_digest(content, "sha256").hexdigest() != sha256:
            raise RuntimeError(f"Source archive checksum mismatch for {repo}")
    with tarfile.open(archive) as source:
        source.extractall(work, filter="data")
    return work / f"{repo.split('/')[-1]}-{revision}"


def build_dotnet(source, destination, notices, target, work):
    project = source / "windows/tools/AgentSpeed.Probe/AgentSpeed.Probe.csproj"
    publish = work / "publish"
    packages = work / "nuget"
    subprocess.run(["dotnet", "publish", str(project), "-c", "Release", "-r", target,
                    "--self-contained", "true", "-p:PublishSingleFile=true",
                    "-p:IncludeNativeLibrariesForSelfExtract=true", "-p:DebugType=None",
                    "-o", str(publish)], check=True, env=dict(os.environ, NUGET_PACKAGES=str(packages)))
    suffix = destination.suffix
    shutil.copy2(publish / f"AgentSpeed.Probe{suffix}", destination)

    assets = json.loads((project.parent / "obj/project.assets.json").read_text(encoding="utf-8"))
    runtime = f"Microsoft.NETCore.App.Runtime.{target}"
    dependency = next(item for item in assets["project"]["frameworks"]["net10.0"]["downloadDependencies"]
                      if item["name"] == runtime)
    version = dependency["version"].strip("[]").split(",")[0].strip()
    package = packages / runtime.lower() / version
    shutil.copyfile(package / "LICENSE.TXT", notices / "dotnet-LICENSE.txt")
    shutil.copyfile(package / "THIRD-PARTY-NOTICES.TXT", notices / "dotnet-THIRD-PARTY-NOTICES.txt")
    zstd = fetch(work, "oleg-st/ZstdSharp", ZSTD_SHARP,
                 "76ea07d544391a8c19ba037f9d775add5e9439cb591275b03f7988a626ec9dc0")
    shutil.copyfile(zstd / "LICENSE", notices / "ZstdSharp-LICENSE.txt")


def build(output, target, *, fixture_output=None):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deskpet-probe-") as temporary:
        work = Path(temporary)
        source = fetch(work, "zoefix/agent-speed", AGENT_SPEED,
                       "eebd1098d8044864c642631ff643fd63495ba22d3a7fcae0436f2834426aca33")
        bundle = work / "bundle"
        notices = bundle / "licenses"
        notices.mkdir(parents=True)
        shutil.copyfile(source / "LICENSE", notices / "AgentSpeed-LICENSE.txt")
        shutil.copyfile(source / "macos/Sources/CZstd/LICENSE", notices / "zstd-LICENSE.txt")
        (bundle / "SOURCE.txt").write_text(
            f"https://github.com/zoefix/agent-speed/tree/{AGENT_SPEED}\n", encoding="utf-8")
        windows = target.startswith("win-")
        binary = bundle / ("AgentSpeedProbe.exe" if windows else "AgentSpeedProbe")
        if target == "macos-arm64":
            command = ["swift", "build", "--package-path", str(source / "macos"), "-c", "release"]
            subprocess.run([*command, "--product", "AgentSpeedProbe"], check=True)
            built = Path(subprocess.check_output([*command, "--show-bin-path"], text=True).strip())
            shutil.copy2(built / binary.name, binary)
        else:
            build_dotnet(source, binary, notices, target, work)
        fixture = source / FIXTURE
        verify(binary, fixture)
        if fixture_output is not None:
            shutil.copyfile(fixture, fixture_output)
        label = target.replace("win-", "windows-")
        return Path(shutil.make_archive(str(output / f"AgentSpeedProbe-{label}"),
                                        "zip" if windows else "gztar", bundle))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fixture-out", type=Path)
    arguments = parser.parse_args()
    target = TARGETS.get((platform.system(), platform.machine().lower()))
    if target is None:
        parser.error("Use a native macOS arm64, Windows x64, or Linux x64/arm64 build machine.")
    print(build(arguments.output.resolve(), target, fixture_output=arguments.fixture_out))
