"""Consume the original controller's aggregate collector over its watch API."""
import asyncio
from collections import deque
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import time


class ReferenceCollector:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / "host/token_collector/pyproject.toml").is_file():
            raise ValueError("请提供原桌面屏项目的目录。")
        self.process = None
        self.reader = None
        self.latest = None
        self.trend = deque()

    async def sample(self):
        if self.reader is not None and self.reader.done():
            await self.close()
        if self.process is None:
            self.process = await asyncio.create_subprocess_exec(
                "uv", "run", "--project", "host/token_collector", "--locked",
                "python", "-m", "host.token_collector",
                cwd=self.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=65536,
            )
            request = {"operation": "watch", "auto": True, "interval_seconds": 1}
            self.process.stdin.write(json.dumps(request).encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
            self.reader = asyncio.create_task(self._read())
        return self.normalize(self.latest, time.time()) if self.latest is not None else None

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if isinstance(value, dict) and isinstance(value.get("total"), dict):
                    self.latest = value
        except (OSError, ValueError):
            pass
        finally:
            self.latest = None

    def normalize(self, value, now):
        reading = value["total"]
        stamp = value.get("captured_at_ms")
        if type(stamp) is not int:
            return None
        captured = stamp / 1000
        rate, sessions = reading.get("output_tps"), reading.get("active_sessions")
        if (reading.get("speed_state") not in ("measured", "estimated", "partial") or
                type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0 or
                not -1 <= now - captured <= 3):
            return None
        rate10 = min(99999, round(rate * 10))
        if not self.trend or self.trend[-1][0] != captured:
            self.trend.append((captured, rate10))
        while self.trend and self.trend[0][0] < now - 60:
            self.trend.popleft()
        trend = [0] * 12
        for stamp, recent_rate in self.trend:
            index = min(11, max(0, 11 - int((now - stamp) / 5)))
            trend[index] = recent_rate
        return {"rate10": rate10, "sessions": min(255, max(0, sessions)) if type(sessions) is int else None,
                "trend": trend, "quotas": []}

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
