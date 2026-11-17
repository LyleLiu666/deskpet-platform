"""Capture only quota fields while passing a status-line input through unchanged."""

import argparse
from contextlib import closing, contextmanager
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
ANTIGRAVITY_REFRESH_SECONDS = 120
READERS = {"claude_code": claude_quota.read_statusline_quota,
           "antigravity": antigravity_quota.read_statusline_quota}


class QuotaStore:
    """Share the latest sanitized reading, without storing the status-line input."""

    def __init__(self, path):
        if os.name == "nt":
            from .windows_private import state_path
            path = state_path(path)
        self.path = Path(path)

    @contextmanager
    def _write_connection(self):
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
            yield db

    def record(self, source, payload, observed_at):
        reading = READERS[source](payload, observed_at)
        with self._write_connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS snapshots "
                       "(source TEXT PRIMARY KEY, observed REAL NOT NULL, reading TEXT NOT NULL)")
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                       "observed=excluded.observed, reading=excluded.reading "
                       "WHERE excluded.observed >= snapshots.observed",
                       (source, observed_at, json.dumps(reading, ensure_ascii=True, separators=(",", ":"))))

    def set_antigravity_refresh(self, enabled):
        with self._write_connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS antigravity_refresh "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL)")
            return bool(db.execute("""INSERT INTO antigravity_refresh VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled WHERE enabled!=excluded.enabled""",
                                   (enabled,)).rowcount)

    def refresh_antigravity(self):
        """Coalesce CLI attempts across all processes using this quota store."""
        if not self.path.is_file() or self.path.is_symlink():
            return
        source = "antigravity"
        now = time.time()
        if self.read(source, now, max_age=ANTIGRAVITY_REFRESH_SECONDS):
            return
        with self._write_connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS antigravity_refresh "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS refresh_attempts "
                       "(source TEXT PRIMARY KEY, started REAL NOT NULL)")
            claimed = db.execute("""INSERT INTO refresh_attempts
                SELECT ?, ? FROM antigravity_refresh WHERE id=1 AND enabled=1
                ON CONFLICT(source) DO UPDATE SET started=excluded.started
                WHERE excluded.started - refresh_attempts.started >= ?
                   OR refresh_attempts.started - excluded.started >= ?""",
                                 (source, now, ANTIGRAVITY_REFRESH_SECONDS, ANTIGRAVITY_REFRESH_SECONDS)).rowcount
        if not claimed:
            return
        payload = {"quota": antigravity_quota.fetch_usage_buckets()}
        if antigravity_quota.read_statusline_quota(payload, now):
            self.record(source, payload, now)

    def read(self, source, now, *, max_age=MAX_AGE_SECONDS):
        if source not in READERS or not self.path.is_file() or self.path.is_symlink():
            return None
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
                row = db.execute("SELECT observed, reading FROM snapshots WHERE source = ?", (source,)).fetchone()
            if row is None or not 0 <= now - row[0] <= max_age:
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
