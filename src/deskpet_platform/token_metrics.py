"""Read Codex telemetry without sending session content to the device."""
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, OverflowError):
        return 0


def _records(path):
    with path.open("rb") as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length - 524288))
        if length > 524288:
            stream.readline(524288)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            try:
                value = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("payload"), dict):
                yield value


def _usage(payload, modern):
    info = payload.get("usage") if modern else payload.get("info")
    if not modern and isinstance(info, dict):
        info = info.get("last_token_usage")
    if not isinstance(info, dict):
        return None
    value = info.get("output_tokens")
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _quotas(windows, now):
    if not isinstance(windows, dict) or windows.get("limit_id") not in (None, "codex"):
        return None
    result = []
    for key in ("primary", "secondary"):
        window = windows.get(key)
        if isinstance(window, dict):
            used, minutes, reset = (window.get(name) for name in ("used_percent", "window_minutes", "resets_at"))
            if (type(used) in (int, float) and 0 <= used <= 100 and type(minutes) is int and
                    0 < minutes <= 525600 and type(reset) is int and now < reset <= 0xFFFFFFFF):
                result.append([round(100 - used), minutes, reset])
    return result or None


def _telemetry(path):
    records = []
    for event in _records(path):
        kind, payload = event.get("type"), event["payload"]
        modern = kind == "token_usage_record"
        if not modern and (kind != "event_msg" or payload.get("type") != "token_count"):
            continue
        timestamp = _timestamp(event.get("timestamp"))
        amount = _usage(payload, modern)
        key = payload.get("response_id")
        rates = payload.get("rate_limits")
        records.append((timestamp, modern, amount, key, rates))
    return records


class TokenMetrics:
    def __init__(self, root):
        self.root = Path(root)
        self.files = {}

    def _paths(self, now):
        day = datetime.fromtimestamp(now, timezone.utc).date()
        paths = []
        for offset in (-1, 0, 1):
            date = day + timedelta(days=offset)
            folder = self.root / date.strftime("%Y/%m/%d")
            for path in folder.glob("*.jsonl"):
                try:
                    info = path.stat()
                except OSError:
                    continue
                if info.st_mtime > now - 3600 and not path.is_symlink():
                    paths.append((info.st_mtime_ns, info.st_size, path))
        return sorted(paths, reverse=True)[:24]

    def sample(self, now=None):
        now = time.time() if now is None else now
        samples = [0] * 12
        sessions = 0
        newest_quota, quotas = 0.0, []
        seen = set()
        scanned = {}
        for changed, size, path in self._paths(now):
            modern, legacy = [], []
            last_activity = 0.0
            try:
                previous = self.files.get(path)
                records = previous[2] if previous and previous[:2] == (changed, size) else _telemetry(path)
                scanned[path] = changed, size, records
                for timestamp, current, amount, key, limits in records:
                    if not now - 3600 <= timestamp <= now + 2:
                        continue
                    last_activity = max(last_activity, timestamp)
                    reading = _quotas(limits, now)
                    if reading is not None and timestamp > newest_quota:
                        newest_quota, quotas = timestamp, reading
                    if amount is not None and timestamp > now - 60:
                        (modern if current else legacy).append((timestamp, amount, key))
            except (OSError, AttributeError, OverflowError, TypeError):
                continue
            sessions += last_activity > now - 60
            times = sorted(entry[0] for entry in modern)
            for stamp, amount, key in legacy:
                nearby = bisect_left(times, stamp - 2)
                if nearby == len(times) or times[nearby] > stamp + 2:
                    modern.append((stamp, amount, key))
            for stamp, amount, key in modern:
                if isinstance(key, str):
                    if key in seen:
                        continue
                    seen.add(key)
                index = min(11, max(0, 11 - int((now - stamp) / 5)))
                samples[index] += amount
        self.files = scanned
        rates = [min(99999, amount * 2) for amount in samples]
        rate10 = min(99999, sum(samples[-2:]))
        return {"rate10": rate10, "sessions": sessions, "trend": rates, "quotas": quotas}
