"""Capture only quota fields while passing a status-line input through unchanged."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from . import antigravity_quota, claude_quota


MAX_INPUT_BYTES = 1024 * 1024
MAX_AGE_SECONDS = 300
READERS = {"claude_code": claude_quota.read_statusline_quota,
           "antigravity": antigravity_quota.read_statusline_quota}


class QuotaStore:
    """Share the latest sanitized reading, without storing the status-line input."""

    def __init__(self, path):
        if os.name == "nt":
            from .windows_private import state_path
            path = state_path(path)
        self.path = Path(path)

    def record(self, source, payload, observed_at):
        reading = READERS[source](payload, observed_at)
        if os.name == "nt":
            from .windows_private import ensure_private_directory
            self.path.parent.parent.mkdir(parents=True, exist_ok=True)
            ensure_private_directory(self.path.parent)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("quota_store_is_symlink")
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=0.1)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots "
                       "(source TEXT PRIMARY KEY, observed REAL NOT NULL, reading TEXT NOT NULL)")
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                       "observed=excluded.observed, reading=excluded.reading "
                       "WHERE excluded.observed >= snapshots.observed",
                       (source, observed_at, json.dumps(reading, ensure_ascii=True, separators=(",", ":"))))

    def read(self, source, now):
        if source not in READERS or not self.path.is_file() or self.path.is_symlink():
            return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
                row = db.execute("SELECT observed, reading FROM snapshots WHERE source = ?", (source,)).fetchone()
            if row is None or not 0 <= now - row[0] <= MAX_AGE_SECONDS:
                return None
            reading = json.loads(row[1])
            if not isinstance(reading, dict):
                return None
            if source == "claude_code":
                windows = reading.get("quotas")
                if not isinstance(windows, list):
                    return None
                return {"quotas": [window for window in windows if
                                   isinstance(window, list) and len(window) == 3 and
                                   type(window[0]) is int and 0 <= window[0] <= 100 and
                                   type(window[1]) is int and window[1] in (300, 10080) and
                                   type(window[2]) is int and now < window[2] <= 0xFFFFFFFF]}
            filtered = {}
            for identity, value in reading.items():
                if not isinstance(identity, str) or not isinstance(value, dict):
                    continue
                reset = value.get("reset_at")
                if reset is not None and (type(reset) is not int or not now < reset <= 0xFFFFFFFF):
                    continue
                percent = value.get("remaining_percent")
                amount = value.get("remaining_amount")
                if type(percent) is int and 0 <= percent <= 100:
                    filtered[identity] = {"remaining_percent": percent, "reset_at": reset}
                elif type(amount) in (int, float) and 0 <= amount <= 0x1FFFFFFFFFFFFF:
                    filtered[identity] = {"remaining_amount": amount, "reset_at": reset}
            return filtered
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            return None


def capture(source, path, source_stream, destination_stream):
    data = source_stream.read(MAX_INPUT_BYTES + 1)
    try:
        destination_stream.write(data)
        if len(data) > MAX_INPUT_BYTES:
            shutil.copyfileobj(source_stream, destination_stream)
        destination_stream.flush()
    except BrokenPipeError:
        pass
    if len(data) > MAX_INPUT_BYTES:
        return
    try:
        payload = json.loads(data)
        if isinstance(payload, dict):
            QuotaStore(path).record(source, payload, time.time())
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=tuple(READERS))
    parser.add_argument("store", type=Path)
    args = parser.parse_args()
    capture(args.source, args.store, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
