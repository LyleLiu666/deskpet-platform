"""Read the same AgentSpeedProbe output used by the original desktop display."""
import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import math
import os
from pathlib import Path
import platform as platform_info
import subprocess
from sys import platform
import time


def host_platform():
    system = platform_info.system()
    machine = platform_info.machine().lower()
    if system == "Darwin" and machine == "x86_64":
        result = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
                                capture_output=True, text=True, check=False)
        if result.stdout.strip() == "1":
            machine = "arm64"
    if system == "Windows":
        machine = os.environ.get("PROCESSOR_ARCHITEW6432", machine).lower()
    machine = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    assets = {
        ("Darwin", "arm64"): "macos-arm64.tar.gz",
        ("Windows", "x64"): "windows-x64.zip",
        ("Linux", "arm64"): "linux-arm64.tar.gz",
        ("Linux", "x64"): "linux-x64.tar.gz",
    }
    asset = assets.get((system, machine))
    if system == "Darwin" and platform_info.mac_ver()[0].split(".")[0] in {"10", "11", "12", "13"}:
        asset = None
    if system == "Linux" and platform_info.libc_ver()[0] != "glibc":
        asset = None
    return {"system": system, "architecture": machine,
            "release_probe_asset": f"AgentSpeedProbe-{asset}" if asset else None}


def find_installed_probe(config_path):
    bin_dir = Path(config_path).parent / "bin"
    names = ("AgentSpeedProbe.exe", "AgentSpeed.Probe.exe") if platform == "win32" else ("AgentSpeedProbe",)
    for name in names:
        candidate = bin_dir / name
        if candidate.is_file():
            return candidate
    asset = host_platform()["release_probe_asset"]
    if asset:
        target = asset.removeprefix("AgentSpeedProbe-").removesuffix(".tar.gz").removesuffix(".zip")
        bundled = Path(__file__).with_name("assets") / "token_probes" / target / names[0]
        if bundled.is_file() and (platform == "win32" or os.access(bundled, os.X_OK)):
            return bundled
    return None


class AgentSpeedProbe:
    def __init__(self, executable):
        self.executable = Path(executable).expanduser().resolve()
        if not self.executable.is_file():
            raise ValueError("找不到 AgentSpeedProbe 程序。")
        with self.executable.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()[:12]
        self.metric_version = "agent-speed-output-v1-" + digest
        self.process = self.reader = self.latest = None
        self.speed_state = None
        self.trend = deque()
        self.lock = asyncio.Lock()

    def normalize(self, value, now):
        if not isinstance(value, dict) or value.get("state") != "running":
            return None
        stamp = value.get("t")
        live = value.get("live")
        rate = live.get("output") if isinstance(live, dict) else None
        share = value.get("estimatedShare")
        generating, tools = value.get("generating"), value.get("tools")
        number = lambda item: type(item) in (int, float) and math.isfinite(item)
        if (not number(stamp) or not -1 <= now - stamp / 1000 <= 3 or
                not number(rate) or not 0 <= rate <= 1_000_000 or
                not number(share) or not 0 <= share <= 1 or
                type(generating) is not int or generating < 0 or
                type(tools) is not int or tools < 0 or generating + tools > 9999):
            return None
        captured = stamp / 1000
        rate10 = int(rate * 10 + .5)
        # The current device protocol cannot represent larger rates; do not
        # silently turn them into a different measured value.
        if rate10 > 99999:
            return None
        self.speed_state = "I" if rate == 0 and generating + tools == 0 else "E" if share > 0 else "L"
        if not self.trend or self.trend[-1][0] != captured:
            self.trend.append((captured, rate10))
        while self.trend and self.trend[0][0] < now - 60:
            self.trend.popleft()
        trend = [0] * 12
        for when, amount in self.trend:
            index = min(11, max(0, 11 - int((now - when) / 5)))
            trend[index] = amount
        sessions = generating + tools
        return {"rate10": rate10, "sessions": sessions if sessions <= 255 else None,
                "trend": trend, "quotas": []}

    async def sample(self):
        async with self.lock:
            if self.reader is not None and self.reader.done():
                await self._close()
            if self.process is None:
                self.process = await asyncio.create_subprocess_exec(
                    str(self.executable), "--watch", "86400", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
                )
                self.reader = asyncio.create_task(self._read(self.process))
            return self.normalize(self.latest, time.time()) if self.latest is not None else None

    async def _read(self, process):
        try:
            while line := await process.stdout.readline():
                try:
                    value = json.loads(line)
                except (UnicodeError, ValueError):
                    continue
                if not isinstance(value, dict):
                    continue
                live = value.get("live")
                self.latest = {"t": value.get("t"), "state": value.get("state"),
                               "live": {"output": live.get("output")} if isinstance(live, dict) else None,
                               "estimatedShare": value.get("estimatedShare"),
                               "generating": value.get("generating"), "tools": value.get("tools")}
        except (OSError, ValueError):
            pass
        finally:
            self.latest = None

    async def _close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        process, self.process = self.process, None
        self.latest = None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def close(self):
        async with self.lock:
            await self._close()
