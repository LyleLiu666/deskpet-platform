"""Use the documented installer, then start MCP and the bundled probe."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from verify_token_probe import verify as verify_probe


MCP_SMOKE = """
import asyncio
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def verify_pairing_form(url):
    address = urlsplit(url)
    assert (address.scheme, address.hostname) == ("http", "127.0.0.1")
    request = Request(url, data=b"code=fixture-invalid-code",
                      headers={"Origin": f"{address.scheme}://{address.netloc}"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            raise RuntimeError(f"Invalid code returned HTTP {response.status}")
    except HTTPError as error:
        with error:
            body = error.read()
            assert error.code == 400 and b'<form method="post">' in body and b'fixture-invalid-code' not in body


async def verify():
    params = StdioServerParameters(command=sys.argv[1], args=[
        "--config", sys.argv[2], "mcp", "--no-token-probe",
    ], env=dict(os.environ))
    async with asyncio.timeout(15), stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"desk_list_devices", "desk_pair_begin", "desk_enable_codex_notifications"} <= names, names
            portal = await session.call_tool("desk_pair_begin", {"open_browser": False})
            assert not portal.isError and portal.structuredContent
            await asyncio.to_thread(verify_pairing_form, portal.structuredContent["url"])
            status = await session.call_tool("desk_pair_status")
            assert not status.isError and status.structuredContent["state"] == "waiting"


asyncio.run(verify())
"""


def verify(archive, fixture):
    with tempfile.TemporaryDirectory(prefix="DeskPet 托肯 clean install ") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        shutil.unpack_archive(archive, source, filter="data" if archive.name.endswith(".tar.gz") else None)
        package, = source.iterdir()
        environment = package / ".venv"
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        desktop = scripts / ("deskpet-platform.exe" if os.name == "nt" else "deskpet-platform")
        if os.name == "nt":
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(package / "install-windows.ps1")], cwd=package, check=True, timeout=300)
        elif sys.platform == "darwin":
            subprocess.run(["bash", "install-macos.command"], cwd=package, check=True, timeout=300)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, timeout=60)
            subprocess.run([str(python), "-m", "pip", "install", "."], cwd=package, check=True, timeout=300)
        version = subprocess.check_output([str(desktop), "--version"], cwd=root, encoding="utf-8", timeout=15).strip()
        expected = package.name.removeprefix("deskpet_platform-")
        if version.rsplit(" ", 1)[-1] != expected:
            raise RuntimeError(f"Installed {version!r}, expected package version {expected!r}")
        config = root / "fresh-state" / "device.json"
        invocation = [str(desktop), "--config", str(config), "doctor", "--client", "codex"]
        env = dict(os.environ, CODEX_HOME=str(root / "codex"), USERPROFILE=str(root), HOME=str(root),
                   APPDATA=str(root), LOCALAPPDATA=str(root), XDG_CONFIG_HOME=str(root), XDG_DATA_HOME=str(root),
                   DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(root / "runtime"))
        json_env = dict(env, PYTHONIOENCODING="cp1252")
        report = json.loads(subprocess.check_output(invocation, cwd=root, env=json_env, timeout=15))
        if report["speed"]["status"] != "present":
            raise RuntimeError(f"Bundle probe is not discoverable: {report['speed']}")
        probe = Path(report["speed"]["path"])
        if not probe.is_relative_to(environment) or config.parent.exists():
            raise RuntimeError("Discovery touched device configuration or selected an external probe")
        if json.loads(subprocess.check_output(
                [*invocation, "--no-token-probe"], cwd=root, env=json_env, timeout=15))["speed"]["status"] != "disabled":
            raise RuntimeError("Could not disable the bundled probe")
        subprocess.run([str(python), "-c", MCP_SMOKE, str(desktop), str(config)], cwd=root, env=env,
                       check=True, timeout=30)
        verify_probe(probe, fixture)
        provider_check = Path(__file__).with_name("verify_provider_token_probe.py").resolve()
        subprocess.run([str(python), str(provider_check), str(probe)], cwd=root, env=env,
                       check=True, timeout=150)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    verify(args.archive.resolve(), args.fixture.resolve())
