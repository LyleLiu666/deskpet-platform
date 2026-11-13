"""Optional, device-specific display settings and local computer utilisation."""
import json
import math
import os
from pathlib import Path
import tempfile
import time

import psutil


METRICS = ("cpu", "memory")


class DisplayPreferences:
    def __init__(self, path):
        self.path = Path(path)
        self.value = {"cpu": False, "memory": False}
        self.pending_fields = frozenset()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or any(type(saved.get(key)) is not bool for key in METRICS):
                return
            pending = saved.get("pending_fields", METRICS if saved.get("pending_device") is True else ())
            if not isinstance(pending, (list, tuple)) or any(key not in METRICS for key in pending):
                return
            self.value = {key: saved[key] for key in METRICS}
            self.pending_fields = frozenset(pending)
        except (OSError, ValueError, UnicodeError):
            pass

    def get(self):
        return self.value.copy()

    def update(self, *, cpu=None, memory=None, queue=False, synchronized=False):
        requested = {key: value for key, value in (("cpu", cpu), ("memory", memory)) if value is not None}
        if any(type(value) is not bool for value in requested.values()):
            raise ValueError("CPU 和内存显示开关须为开或关。")
        value = {**self.value, **requested}
        pending = self.pending_fields
        if queue:
            pending |= requested.keys()
        elif synchronized:
            pending = frozenset()
        if value == self.value and pending == self.pending_fields:
            return self.get()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".display-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({**value, "pending_fields": sorted(pending)}, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.value = value
        self.pending_fields = frozenset(pending)
        return self.get()


class ComputerMetrics:
    def __init__(self, source=psutil, clock=time.monotonic):
        self.source, self.clock = source, clock
        self.previous = None

    @staticmethod
    def _percent(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            return None
        return min(100, int(value + .5))

    def _cpu(self):
        try:
            counters = self.source.cpu_times()._asdict()
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in counters.values()):
                raise ValueError("invalid CPU counters")
            total = sum(counters.values()) - counters.get("guest", 0) - counters.get("guest_nice", 0)
            idle = counters["idle"] + counters.get("iowait", 0)
            now = self.clock()
            previous, self.previous = self.previous, (counters, total, idle, now)
            if previous is None:
                return None
            old, old_total, old_idle, then = previous
            if (counters.keys() != old.keys() or any(counters[key] < old[key] for key in counters) or
                    not 0 <= now - then <= 10 or total <= old_total):
                return None
            return self._percent(100 * (1 - (idle - old_idle) / (total - old_total)))
        except (psutil.Error, OSError, AttributeError, KeyError, ValueError, TypeError):
            self.previous = None
            return None

    def sample(self, *, cpu, memory):
        result = {}
        if cpu:
            result["cpu"] = self._cpu()
        else:
            self.previous = None
        if memory:
            try:
                result["memory"] = self._percent(self.source.virtual_memory().percent)
            except (psutil.Error, OSError, AttributeError, ValueError, TypeError):
                result["memory"] = None
        return result
