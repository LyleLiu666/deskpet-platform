"""Persistent cursor journal and the canonical DeskPet usage projection."""
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import struct
import zlib

from .analytics_collectors import CodexRollout, MAX_AMOUNT, jsonl_events, log_paths, opencode_events
from .antigravity_metadata import companion_database, metadata_revision, read_metadata
from .notifications import NotificationStore
from .quota_projection import project_quota

SOURCES = ("codex", "claude_code", "antigravity_gemini", "antigravity_third_party", "opencode", "workbuddy")
CAPACITY = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cursors (
  path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, size INTEGER, mtime INTEGER,
  offset INTEGER, session TEXT, boundary BLOB
);
CREATE INDEX IF NOT EXISTS file_cursors_identity ON file_cursors(device, inode);
CREATE TABLE IF NOT EXISTS events (
  file TEXT NOT NULL, source INTEGER NOT NULL, session TEXT NOT NULL, key TEXT NOT NULL,
  stamp REAL NOT NULL, amount INTEGER, turn INTEGER, title TEXT, running INTEGER,
  codex_total INTEGER, codex_legacy INTEGER NOT NULL,
  PRIMARY KEY(file, source, session, key)
);
CREATE INDEX IF NOT EXISTS events_source ON events(source, session, key);
CREATE INDEX IF NOT EXISTS events_file_stamp ON events(file, stamp);
CREATE INDEX IF NOT EXISTS events_codex_total ON events(source, session, codex_total, amount);
CREATE INDEX IF NOT EXISTS events_codex_mirror_time ON events(session, amount, stamp, codex_total)
  WHERE source=0 AND NOT codex_legacy AND amount IS NOT NULL;
CREATE TABLE IF NOT EXISTS usage_cursors (
  path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, size INTEGER, mtime INTEGER
);
CREATE TABLE IF NOT EXISTS codex_rollout_cursors (
  path TEXT PRIMARY KEY, device INTEGER, inode INTEGER, size INTEGER, mtime INTEGER,
  offset INTEGER, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS antigravity_metadata_cursors (
  path TEXT PRIMARY KEY, revision TEXT
);
CREATE TABLE IF NOT EXISTS event_inputs (
  file TEXT NOT NULL, source INTEGER NOT NULL, session TEXT NOT NULL, key TEXT NOT NULL,
  input_tokens INTEGER, cached_input_tokens INTEGER,
  PRIMARY KEY(file, source, session, key)
);
CREATE TABLE IF NOT EXISTS sync_receipts (device TEXT PRIMARY KEY, observed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quota_snapshots (
  source INTEGER PRIMARY KEY, observed_at INTEGER NOT NULL, snapshot TEXT NOT NULL
);
"""

INSERT_EVENT = """
INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(file, source, session, key) DO UPDATE SET
  stamp=max(events.stamp,excluded.stamp), amount=coalesce(excluded.amount,events.amount),
  turn=excluded.turn, title=excluded.title, running=excluded.running,
  codex_total=excluded.codex_total,codex_legacy=excluded.codex_legacy
"""

# Fully described Codex reports are reconciled before insertion. Sparse retained
# rows still use their original cumulative or nearby matching rules.
EVENTS = """
WITH relevant AS (
  SELECT e.* FROM events e
  WHERE e.source=:source AND e.stamp <= :until
  AND (NOT e.codex_legacy OR (
    NOT EXISTS (
      SELECT 1 FROM events m
      WHERE m.source=0 AND m.session=e.session AND NOT m.codex_legacy
        AND m.codex_total=e.codex_total AND m.amount=e.amount
    )
    AND NOT EXISTS (
      SELECT 1 FROM events m
      WHERE m.source=0 AND m.session=e.session AND NOT m.codex_legacy AND m.amount=e.amount
        AND m.stamp>e.stamp-2 AND m.stamp<e.stamp+2
        AND (m.codex_total IS NULL OR e.codex_total IS NULL)
    )
  ))
  UNION ALL
  SELECT p.* FROM events p
  WHERE :source IN (2,3) AND p.source=-1 AND p.turn AND p.stamp <= :until
  AND :source=(
    SELECT CASE WHEN next.turn THEN -1 ELSE next.source END
    FROM events next
    WHERE next.file=p.file AND next.stamp >= p.stamp AND next.stamp <= :until
      AND cast(substr(next.key, instr(next.key, ':')+1) AS integer) > cast(substr(p.key, 8) AS integer)
    ORDER BY cast(substr(next.key, instr(next.key, ':')+1) AS integer) LIMIT 1
  )
), ranked AS (
  SELECT e.*, row_number() OVER (
    PARTITION BY session, key ORDER BY (amount IS NOT NULL) DESC, stamp DESC, file
  ) AS choice
  FROM relevant e
), observed AS (
  SELECT r.*, i.input_tokens, i.cached_input_tokens
  FROM ranked r LEFT JOIN event_inputs i USING(file, source, session, key)
  WHERE choice=1
)
"""

INPUTS = """
  CASE WHEN count(input_tokens)=count(amount) THEN wire_sum(input_tokens) END,
  CASE WHEN count(cached_input_tokens)=count(amount) THEN wire_sum(cached_input_tokens) END
"""


class _WireSum:
    def __init__(self):
        self.value = 0
        self.known = False

    def step(self, amount):
        if amount is not None:
            self.known = True
            self.value = min(self.value + amount, MAX_AMOUNT)

    def finalize(self):
        return self.value if self.known else None


def _wire_crc(unit, quota, days, sessions):
    credits = quota["credits"].encode("ascii")
    snapshot = struct.pack("<IBBH12s", quota["observed_at"], quota["low_warning"],
                           len(quota["windows"]), quota["reset_cards"], credits)
    windows = [struct.pack("<BBHII", left, 1, 0, mins, reset or 0) for left, mins, reset in quota["windows"]]
    snapshot += b"".join(windows).ljust(24, b"\0")
    day_bytes = b"".join(struct.pack("<IQHH", *day, 1) for day in days).ljust(CAPACITY * 16, b"\0")
    session_bytes = b"".join(struct.pack("<15sBIQHH48s", item["key"].encode("ascii"), item["state"],
                                        item["updated_at"], item["output_tokens"], item["turns"], 0,
                                        item["title"].encode("utf-8")) for item in sessions)
    session_bytes = session_bytes.ljust(CAPACITY * 80, b"\0")
    return f"{zlib.crc32(snapshot):08x}", f"{zlib.crc32(bytes([unit]) + day_bytes + session_bytes):08x}"


def _wire_usage_crc(day_usage, session_usage):
    crc = 0
    for group in (day_usage, session_usage):
        packed = b"".join(struct.pack("<QQ", 0 if inputs is None else inputs + 1,
                                     0 if cache is None else cache + 1)
                          for inputs, cache in group.values())
        crc = zlib.crc32(packed.ljust(CAPACITY * 16, b"\0"), crc)
    return f"{crc:08x}"


class AnalyticsStore:
    def __init__(self, path, *, home=None, timezone=None):
        if os.name == "nt":
            from .windows_private import state_path
            path = state_path(path)
        self.path = Path(path)
        self.home = Path(home) if home is not None else Path.home()
        self.timezone = timezone

    @contextmanager
    def _connect(self):
        if os.name == "nt":
            from .windows_private import ensure_private_directory
            self.path.parent.parent.mkdir(parents=True, exist_ok=True)
            ensure_private_directory(self.path.parent)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
        if self.path.is_symlink():
            raise ValueError("analytics_store_is_symlink")
        fd = os.open(self.path, flags | os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.executescript(SCHEMA)
            db.create_aggregate("wire_sum", 1, _WireSum)
            db.create_function("natural_day", 1, lambda stamp: int(
                datetime.fromtimestamp(stamp, self.timezone).strftime("%Y%m%d")), deterministic=True)
            yield db

    @staticmethod
    def _insert(db, path, event):
        db.execute(INSERT_EVENT, (path, event.source, event.session, event.key, event.timestamp, event.amount,
                                  event.turn, event.title, event.running, event.codex_total, event.codex_legacy))
        if event.amount is not None:
            key = path, event.source, event.session, event.key
            if event.input_tokens is not None:
                db.execute("INSERT OR REPLACE INTO event_inputs VALUES (?,?,?,?,?,?)",
                           (*key, event.input_tokens, event.cached_input_tokens))
            else:
                db.execute("DELETE FROM event_inputs WHERE file=? AND source=? AND session=? AND key=?", key)

    @staticmethod
    def _forget_relocated_codex_log(db, path, stream, identity):
        # An archive rename can leave old-format rows behind under the vanished
        # path. Require the same inode, name and saved bytes before replacing it.
        aliases = db.execute("SELECT path,offset,boundary FROM file_cursors "
                             "WHERE device=? AND inode=? AND path!=?",
                             (*identity[:2], str(path))).fetchall()
        for old_path, offset, boundary in aliases:
            if Path(old_path).name != path.name or os.path.lexists(old_path) or not 0 < offset <= identity[2]:
                continue
            stream.seek(max(0, offset - 64))
            if stream.read(min(64, offset)) != boundary:
                continue
            db.execute("DELETE FROM events WHERE file=?", (old_path,))
            db.execute("DELETE FROM event_inputs WHERE file=?", (old_path,))
            db.execute("DELETE FROM file_cursors WHERE path=?", (old_path,))
            db.execute("DELETE FROM usage_cursors WHERE path=?", (old_path,))
            db.execute("DELETE FROM codex_rollout_cursors WHERE path=?", (old_path,))

    def _scan_file(self, db, kind, path, default_session, earliest):
        if path.is_symlink():
            return
        try:
            stat = path.stat()
        except OSError:
            return
        if stat.st_mtime < earliest:
            return
        name = str(path)
        previous = db.execute("SELECT device,inode,size,mtime,offset,session,boundary FROM file_cursors "
                              "WHERE path=?", (name,)).fetchone()
        # Older receivers can still use the base tables. Rescan when their
        # cursor has advanced farther than the input data, including on upgrade.
        usage_cursor = db.execute("SELECT device,inode,size,mtime FROM usage_cursors WHERE path=?",
                                  (name,)).fetchone()
        if previous and previous[:4] != usage_cursor:
            previous = None
        companion = revision = previous_revision = None
        if kind == "antigravity":
            companion = companion_database(path.absolute())
            revision = metadata_revision(companion)
            previous_revision = db.execute("SELECT revision FROM antigravity_metadata_cursors WHERE path=?",
                                           (name,)).fetchone()
            if previous_revision is None or (revision is not None and revision != previous_revision[0]):
                # Native metadata can arrive after the JSONL step, even
                # when the transcript itself hasn't changed since the last scan.
                previous = None
        codex = None
        if kind == "codex":
            saved = db.execute("SELECT device,inode,size,mtime,offset,state FROM codex_rollout_cursors WHERE path=?",
                               (name,)).fetchone()
            if previous and saved and saved[:5] == previous[:5]:
                try:
                    codex = CodexRollout(**json.loads(saved[5]))
                except (TypeError, ValueError):
                    pass
            if codex is None:
                # Rebuild readable Codex files once with the new reconciliation
                # rules. An older writer may also have advanced the base cursor.
                previous = None
        identity = stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        if previous and previous[:4] == identity:
            return
        metadata = None
        if revision is not None:
            try:
                metadata = read_metadata(companion)
            except (OSError, sqlite3.Error):
                if previous_revision is not None and previous_revision[0] is not None:
                    return
                revision = None
        try:
            stream = path.open("rb")
        except OSError:
            return
        with stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != identity[:2]:
                return
            offset, session = 0, default_session
            if (previous and previous[:2] == identity[:2] and identity[2] > previous[2]
                    and not (codex and codex.fork_prefix_pending)):
                candidate = previous[4]
                stream.seek(max(0, candidate - 64))
                if stream.read(min(64, candidate)) == previous[6]:
                    offset, session = candidate, previous[5]
            if not offset:
                db.execute("DELETE FROM events WHERE file=?", (name,))
                db.execute("DELETE FROM event_inputs WHERE file=?", (name,))
                if kind == "codex":
                    self._forget_relocated_codex_log(db, path, stream, identity)
                    codex = CodexRollout()
            stream.seek(offset)
            for offset, session, event in jsonl_events(kind, stream, session, codex=codex,
                                                      antigravity_metadata=metadata):
                if event is not None and event.session:
                    self._insert(db, name, event)
            stream.seek(max(0, offset - 64))
            boundary = stream.read(min(64, offset))
            db.execute("INSERT OR REPLACE INTO file_cursors VALUES (?,?,?,?,?,?,?,?)",
                       (name, *identity, offset, session, boundary))
            db.execute("INSERT OR REPLACE INTO usage_cursors VALUES (?,?,?,?,?)", (name, *identity))
            if codex is not None:
                db.execute("INSERT OR REPLACE INTO codex_rollout_cursors VALUES (?,?,?,?,?,?,?)",
                           (name, *identity, offset, json.dumps(asdict(codex))))
            if kind == "antigravity":
                db.execute("INSERT OR REPLACE INTO antigravity_metadata_cursors VALUES (?,?)", (name, revision))

    def collect(self, now):
        today = datetime.fromtimestamp(now, self.timezone).date()
        begin = today - timedelta(days=CAPACITY - 1)
        earliest = datetime.combine(begin, datetime.min.time(), self.timezone).timestamp()
        with self._connect() as db:
            # The cursor and its events must commit together, including when
            # two paired receivers happen to request a refresh concurrently.
            db.execute("BEGIN IMMEDIATE")
            for kind, path, session in log_paths(self.home):
                self._scan_file(db, kind, path, session, earliest)
            self._scan_opencode(db, earliest)

    def _scan_opencode(self, db, earliest):
        root = Path(os.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        path = root / "opencode/opencode.db"
        if not path.is_file() or path.is_symlink():
            return
        # The app can update previous message usage or titles in place. A
        # read-only source query refreshes its bounded recent-session snapshot.
        db.execute("SAVEPOINT opencode")
        try:
            db.execute("DELETE FROM events WHERE file=?", (str(path),))
            db.execute("DELETE FROM event_inputs WHERE file=?", (str(path),))
            for event in opencode_events(path, earliest):
                self._insert(db, str(path), event)
        except (OSError, sqlite3.Error, ValueError):
            db.execute("ROLLBACK TO opencode")
        db.execute("RELEASE opencode")

    def collect_quota(self, quota_store, *, codex=None):
        """Archive only provider observations, with their original time."""
        for provider, sources in (("claude_code", (1,)), ("antigravity", (2, 3))):
            observation = quota_store.observation(provider)
            if observation:
                observed, reading = observation
                for source in sources:
                    projected = project_quota(SOURCES[source], now=observed, **{
                        "claude" if source == 1 else "antigravity": reading,
                    })
                    self.record_quota(source, observed, projected)
        if codex:
            observed, reading = codex
            self.record_quota(0, observed, project_quota("codex", now=observed, codex=reading))

    def record_quota(self, source, observed_at, projected):
        if source not in range(4) or not 1577836800 <= observed_at <= 4102444799:
            return
        windows = projected.get("quotas", [])
        if not windows:
            return
        credits = projected.get("credits")
        quota = {
            "observed_at": int(observed_at),
            "low_warning": min(row[0] for row in windows) <= 15,
            "reset_cards": min(projected.get("reset_cards") or 0, 65535),
            "credits": (str(credits).encode("ascii", errors="ignore")[:11].decode() if credits is not None else ""),
            "windows": windows[:2],
        }
        with self._connect() as db:
            db.execute("INSERT INTO quota_snapshots VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET "
                       "observed_at=excluded.observed_at,snapshot=excluded.snapshot "
                       "WHERE excluded.observed_at>=quota_snapshots.observed_at AND excluded.snapshot!=quota_snapshots.snapshot",
                       (source, int(observed_at), json.dumps(quota)))

    def record_sync(self, device, stamp):
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO sync_receipts VALUES (?,?)", (str(device), stamp))

    def compute_source_payload(self, source, now):
        if not 0 <= source < len(SOURCES):
            raise ValueError("invalid_analytics_source")
        now_day = datetime.fromtimestamp(now, self.timezone).date()
        first_day = int((now_day - timedelta(days=CAPACITY - 1)).strftime("%Y%m%d"))
        bind = dict(source=source, until=now + 2, first_day=first_day,
                    today=int(now_day.strftime("%Y%m%d")), active_since=now - 300)
        with self._connect() as db:
            db.execute("BEGIN")
            day_rows = db.execute(EVENTS + f"""
                SELECT natural_day(stamp) AS day, wire_sum(amount), count(DISTINCT session), {INPUTS}
                FROM observed WHERE natural_day(stamp) BETWEEN :first_day AND :today
                GROUP BY day HAVING count(amount)>0 ORDER BY day DESC LIMIT 30
            """, bind).fetchall()
            rows = db.execute(EVENTS + f"""
                , session_events AS (
                  SELECT observed.*,
                    first_value(title) OVER (
                      PARTITION BY session ORDER BY title!='' DESC, stamp DESC, key DESC
                    ) AS latest_title,
                    first_value(CASE WHEN running AND stamp > :active_since THEN 1 ELSE 0 END) OVER (
                      PARTITION BY session ORDER BY running IS NOT NULL DESC, stamp DESC, key
                    ) AS latest_running
                  FROM observed
                )
                SELECT session,max(stamp),wire_sum(amount),sum(turn),max(latest_title),max(latest_running),{INPUTS}
                FROM session_events GROUP BY session HAVING count(amount)>0
                ORDER BY max(stamp) DESC
            """, bind).fetchall()
            entry = db.execute("SELECT snapshot FROM quota_snapshots WHERE source=?", (source,)).fetchone()

        days = [(date, amount, min(count, 65535)) for date, amount, count, _, _ in day_rows]
        day_usage = {date: (inputs, cache) for date, _, _, inputs, cache in day_rows}
        fallback = ("Codex", "Claude Code", "Antigravity Gemini", "Antigravity 第三方", "OpenCode", "WorkBuddy")[source]
        identity = "antigravity" if source in (2, 3) else SOURCES[source]
        sessions, session_usage = [], {}
        for session, stamp, amount, turns, title, running, inputs, cache in rows:
            key = NotificationStore._session_key(identity, session).hex()
            session_usage[key] = (inputs, cache)
            sessions.append(dict(key=key, title=title or fallback, updated_at=int(stamp), output_tokens=amount,
                                 turns=min(turns or 0, 65535), state=int(bool(running))))
        sessions.sort(key=lambda item: (-item["updated_at"], item["key"]))
        sessions = sessions[:CAPACITY]
        session_usage = {item["key"]: session_usage[item["key"]] for item in sessions}
        quota = (json.loads(entry[0]) if entry else
                 dict(observed_at=0, low_warning=False, reset_cards=0, credits="", windows=[]))
        unit = 0
        quota_crc, rollup_crc = _wire_crc(unit, quota, days, sessions)
        return dict(unit_kind=unit, quota=quota, quota_crc=quota_crc, rollup_crc=rollup_crc,
                    days=days, sessions=sessions, day_usage=day_usage, session_usage=session_usage,
                    usage_crc=_wire_usage_crc(day_usage, session_usage))
