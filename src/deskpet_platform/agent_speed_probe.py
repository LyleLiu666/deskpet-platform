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
import tempfile
from sys import platform
import time

from .device_lock import DeviceLock
from .session_titles import lookup_session_title

AGENT_NAMES = {
    "codex": "Codex", "claudeCode": "Claude Code", "claudeDesktop": "Claude Desktop",
    "deepseek": "DeepSeek", "opencode": "OpenCode", "gemini": "Gemini",
    "antigravity": "Antigravity", "workbuddy": "WorkBuddy", "workbuddyAi": "WorkBuddy AI",
    "zcode": "ZCode", "kimi": "Kimi", "grokBuild": "Grok Build", "omp": "OMP",
    "pi": "Pi", "openclaw": "OpenClaw", "hermes": "Hermes", "droid": "Droid", "devin": "Devin",
}


def _display_title(value):
    if not isinstance(value, str):
        return ""
    title = " ".join(value.split())[:48]
    if not title or not title.isprintable():
        return ""
    while len(title.encode()) > 48 or len(json.dumps(title, ensure_ascii=True)) > 84:
        title = title[:-1]
    return title


def active_session_rows(agents, lookup):
    if not isinstance(agents, list):
        return None
    sessions = {}
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("phase") not in ("generating", "tools"):
            continue
        source, identity = agent.get("source"), agent.get("id")
        if not isinstance(source, str) or not isinstance(identity, str):
            continue
        name = AGENT_NAMES.get(source, source[:16])
        if name and name.isascii() and name.isprintable() and identity:
            identity = identity.removesuffix(".jsonl").removesuffix(".json").removesuffix(".log")
            key = (name, source, identity)
            sessions[key] = _display_title(agent.get("title") or agent.get("name")) or sessions.get(key, "")
    return [[name, sessions[(name, source, identity)] or _display_title(lookup(source, identity))]
            for name, source, identity in sorted(sessions)[:6]]


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
        self.lease = None
        # Older workers can still be running with the aggregate-only cache.
        self.cache_lock = DeviceLock("agent-speed-v3:" + self.metric_version)
        self.shared_path = self.cache_lock.root / (self.cache_lock.name + ".json")
        self.request_path = self.cache_lock.root / (self.cache_lock.name + ".request")
        self.speed_state = None
        self.last_requested = None
        self.trend = deque()
        self.lock = asyncio.Lock()
        self.session_titles = {}

    def _session_rows(self, agents):
        now = time.monotonic()

        def lookup(source, identity):
            key = (source, identity)
            until, title = self.session_titles.get(key, (0, None))
            if until <= now:
                title = lookup_session_title(source, identity)
                self.session_titles[key] = (now + (20 if title else 5), title)
            return title

        self.session_titles = {key: value for key, value in self.session_titles.items() if value[0] > now}
        return active_session_rows(agents, lookup)

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
        snapshot = {"rate10": rate10, "sessions": sessions if sessions <= 255 else None,
                    "trend": trend, "quotas": []}
        rows = value.get("session_titles")
        if isinstance(rows, list) and len(rows) <= sessions <= 255:
            snapshot["session_titles"] = rows
        return snapshot

    async def sample(self):
        async with self.lock:
            self.last_requested = time.monotonic()
            if self.reader is not None and self.reader.done():
                await self._close()
            if self.process is None:
                try:
                    self.lease = self.cache_lock.__enter__()
                except RuntimeError as error:
                    if not str(error).startswith("device_busy:"):
                        raise
                    self._request_shared()
                    self.latest = self._read_shared()
                    return self.normalize(self.latest, time.time()) if self.latest is not None else None
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        str(self.executable), "--watch", "86400", stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
                    )
                except BaseException:
                    self.lease.__exit__(None, None, None)
                    self.lease = None
                    raise
                self.reader = asyncio.create_task(self._read(self.process))
            return self.normalize(self.latest, time.time()) if self.latest is not None else None

    def _request_shared(self):
        try:
            if self.request_path.is_symlink():
                return
            descriptor = os.open(self.request_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(descriptor)
            os.utime(self.request_path, None)
        except OSError:
            pass

    def _shared_requested_recently(self):
        try:
            if self.request_path.is_symlink():
                return False
            age = time.time() - self.request_path.stat().st_mtime
            return -1 <= age < 35
        except OSError:
            return False

    def _read_shared(self):
        try:
            if self.shared_path.is_symlink() or self.shared_path.stat().st_size > 4096:
                return None
            with self.shared_path.open("r", encoding="utf-8") as source:
                value = json.load(source)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _write_shared(self, value):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.shared_path.parent,
                                             prefix=".speed-", delete=False) as target:
                name = target.name
                json.dump(value, target, separators=(",", ":"))
            os.replace(name, self.shared_path)
        except OSError:
            if name:
                with suppress(OSError):
                    os.unlink(name)

    async def _read(self, process):
        try:
            while True:
                if (self.last_requested is not None and time.monotonic() - self.last_requested >= 35
                        and not self._shared_requested_recently()):
                    break
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), 2)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
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
                               "generating": value.get("generating"), "tools": value.get("tools"),
                               "session_titles": await asyncio.to_thread(self._session_rows, value.get("agents"))}
                self._write_shared(self.latest)
        except (OSError, ValueError):
            pass
        finally:
            self.latest = None
            await self._stop_process(process)
            if self.lease is not None:
                self.lease.__exit__(None, None, None)
                self.lease = None

    @staticmethod
    async def _stop_process(process):
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()

    async def _close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        process, self.process = self.process, None
        self.latest = None
        await self._stop_process(process)
        if self.lease is not None:
            self.lease.__exit__(None, None, None)
            self.lease = None

    async def close(self):
        async with self.lock:
            await self._close()
