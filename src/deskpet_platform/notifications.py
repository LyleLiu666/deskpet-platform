"""Local notification records; never update Codex's own read state."""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import asyncio
from .channel import TextNotVisible

from .file_lock import acquire, release


SOURCE_NAMES = {"codex": "Codex", "claude_code": "Claude Code", "workbuddy": "WorkBuddy",
                "antigravity": "Antigravity", "opencode": "OpenCode"}

# Several questions in one Codex tool call can be answered independently. A
# newer call in the same turn replaces its previous batch, even after answering.
SUPERSEDED_QUESTION = """n.response_group IS NOT NULL AND EXISTS (
    SELECT 1 FROM notifications AS replacement
    WHERE replacement.source=n.source AND replacement.session=n.session
      AND replacement.parent_turn=n.parent_turn AND replacement.category='response'
      AND replacement.id>n.id AND replacement.response_group IS NOT NULL
      AND replacement.response_group!=n.response_group)"""


def notification_text(item, *, detail=True):
    source = SOURCE_NAMES.get(item["source"], item["source"])
    heading = f"{source} {'需要应答' if item['category'] == 'response' else '完成'}"
    return heading + "\n" + item["title"][:20] if detail else heading


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def session_title(session, cwd):
    try:
        state = (codex_home() / "state_5.sqlite").resolve().as_uri()
        with closing(sqlite3.connect(state + "?mode=ro", uri=True, timeout=0.1)) as db:
            row = db.execute("SELECT name,title FROM threads WHERE id=?", (session,)).fetchone()
        if row:
            for value in row:
                if isinstance(value, str):
                    clean = " ".join(value.split())
                    if clean and clean.isprintable():
                        return clean[:64]
    except (OSError, sqlite3.Error):
        pass
    return (Path(cwd).name if isinstance(cwd, str) else "")[:64] or "Codex"


class NotificationStore:
    def __init__(self, path, *, source=None):
        if os.name == "nt":
            from .windows_private import state_path
            path = state_path(path)
        self.path = Path(path)
        self.source = source

    def _connect(self, *, timeout=2):
        if os.name == "nt":
            from .windows_private import ensure_private_directory
            self.path.parent.parent.mkdir(parents=True, exist_ok=True)
            ensure_private_directory(self.path.parent)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if self.path.is_symlink() or (os.name != "nt" and self.path.stat().st_mode & 0o077):
            raise ValueError("notification_store_requires_private_permissions")
        db = sqlite3.connect(self.path.resolve().as_uri(), uri=True, timeout=timeout)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                title TEXT NOT NULL, created REAL NOT NULL, shown REAL, seen REAL, action_pending INTEGER,
                UNIQUE(source, session, turn))""")
            schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='action_pending'"
            if db.execute(schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN action_pending INTEGER")
            category_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='category'"
            if db.execute(category_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(category_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN category TEXT NOT NULL DEFAULT 'completed'")
            resolved_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='resolved'"
            if db.execute(resolved_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(resolved_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN resolved REAL")
            dismissed_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='dismissed'"
            if db.execute(dismissed_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(dismissed_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN dismissed REAL")
            turn_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='parent_turn'"
            if db.execute(turn_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(turn_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN parent_turn TEXT")
            group_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='response_group'"
            if db.execute(group_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(group_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN response_group TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_resolutions (
                source TEXT NOT NULL, session TEXT NOT NULL, event_id TEXT NOT NULL, resolved REAL NOT NULL,
                PRIMARY KEY(source,session,event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_attention (
                id INTEGER PRIMARY KEY CHECK(id=1), through_id INTEGER NOT NULL)""")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def sync(self, *, initial_history=False):
        """Copy new events into this board's inbox while preserving its receipts."""
        if self.source is None:
            return
        if not self.source.path.exists():
            return
        with closing(self.source._connect()), closing(self._connect()) as db, db:
            db.execute("ATTACH DATABASE ? AS inbox", (self.source.path.resolve().as_uri() + "?mode=ro",))
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS notification_sync (id INTEGER PRIMARY KEY CHECK(id=1), through_id INTEGER NOT NULL)")
            after = db.execute("SELECT COALESCE(MAX(through_id),0) FROM notification_sync").fetchone()[0]
            through = db.execute("SELECT COALESCE(MAX(id),0) FROM inbox.notifications").fetchone()[0]
            if through > after:
                if initial_history and after == 0:
                    db.execute("""INSERT OR IGNORE INTO main.notifications
                        (source,session,turn,title,created,category,parent_turn,resolved,response_group,shown,seen,dismissed,action_pending)
                        SELECT source,session,turn,title,created,category,parent_turn,resolved,response_group,?,seen,dismissed,0
                        FROM inbox.notifications WHERE id>? AND id<=? ORDER BY id""", (time.time(), after, through))
                    db.execute("INSERT OR REPLACE INTO notification_attention VALUES (1,(SELECT COALESCE(MAX(id),0) FROM notifications))")
                else:
                    db.execute("""INSERT OR IGNORE INTO main.notifications
                        (source,session,turn,title,created,category,parent_turn,resolved,response_group)
                        SELECT source,session,turn,title,created,category,parent_turn,resolved,response_group
                        FROM inbox.notifications WHERE id>? AND id<=? ORDER BY id""", (after, through))
                db.execute("INSERT OR REPLACE INTO notification_sync VALUES (1,?)", (through,))
            # Keep old inboxes compatible with acknowledgements that arrive
            # after the event was already copied or before it was recorded.
            db.execute("""UPDATE main.notifications AS target SET resolved=COALESCE(
                    (SELECT resolved FROM inbox.notification_resolutions AS source
                     WHERE (source.source,source.session,source.event_id)=(target.source,target.session,target.turn)),
                    (SELECT resolved FROM inbox.notifications AS source
                     WHERE (source.source,source.session,source.turn)=(target.source,target.session,target.turn)))
                    WHERE target.category='response' AND target.resolved IS NULL""")

    def add_codex_event(self, event):
        session, turn = event.get("session_id"), event.get("turn_id")
        if (event.get("hook_event_name") != "Stop" or
                not all(isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable()
                        for value in (session, turn))):
            raise ValueError("invalid_codex_stop_event")
        title = session_title(session, event.get("cwd"))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(f"""SELECT 1 FROM notifications n WHERE source='codex' AND session=?
                AND parent_turn=? AND category='response' AND resolved IS NULL
                AND NOT ({SUPERSEDED_QUESTION}) LIMIT 1""", (session, turn)).fetchone()
            if pending:
                return None
            return self._insert(db, "codex", session, turn, "completed", title, None)

    def add_agent_event(self, source, session, event_id, category, title, *, parent_turn=None, response_group=None):
        """Record an explicit adapter event, idempotently within one agent session."""
        return self.add_agent_events(source, session, (event_id,), category, title,
                                     parent_turn=parent_turn, response_group=response_group)[0]

    def add_agent_events(self, source, session, event_ids, category, title, *, parent_turn=None, response_group=None):
        """Record a batch in one transaction so a failure cannot expose a partial group."""
        valid_source = (isinstance(source, str) and 0 < len(source) <= 32 and
                        all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in source))
        valid_id = lambda value: isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable()
        event_ids = tuple(event_ids)
        if (not valid_source or not valid_id(session) or not all(map(valid_id, event_ids)) or
                category not in ("completed", "response") or
                (parent_turn is not None and not valid_id(parent_turn)) or
                (response_group is not None and (category != "response" or not parent_turn or not valid_id(response_group))) or
                not isinstance(title, str) or not title.strip() or not title.isprintable()):
            raise ValueError("invalid_agent_event")
        title = " ".join(title.split())[:64]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return [self._insert(db, source, session, event_id, category, title, parent_turn, response_group)
                    for event_id in event_ids]

    @staticmethod
    def _insert(db, source, session, event_id, category, title, parent_turn, response_group=None):
        resolution = db.execute("SELECT resolved FROM notification_resolutions WHERE source=? AND session=? AND event_id=?",
                                (source, session, event_id)).fetchone() if category == "response" else None
        db.execute("""INSERT OR IGNORE INTO notifications
            (source,session,turn,title,created,category,parent_turn,resolved,response_group) VALUES (?,?,?,?,?,?,?,?,?)""",
                   (source, session, event_id, title, time.time(), category, parent_turn,
                    resolution[0] if resolution else None, response_group))
        identity, stored_category, stored_turn, stored_group = db.execute(
            "SELECT id,category,parent_turn,response_group FROM notifications WHERE source=? AND session=? AND turn=?",
            (source, session, event_id)).fetchone()
        if (stored_category != category or stored_turn != parent_turn or
                (stored_group is not None and response_group is not None and stored_group != response_group)):
            raise ValueError("conflicting_agent_event")
        return identity

    @staticmethod
    def _items(rows):
        return [{"id": row[0], "title": row[1], "created_at": row[2],
                 "shown": row[3] is not None, "seen": row[4] is not None,
                 "source": row[5], "category": row[6], "resolved": row[7] is not None} for row in rows]

    def list(self, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("通知数量须为 1 至 100。")
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id,title,created,shown,seen,source,category,resolved FROM notifications ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()
        return self._items(rows)

    def get(self, identity):
        if type(identity) is not int or identity<1:raise ValueError('通知编号无效。')
        with closing(self._connect()) as db:
            row=db.execute('SELECT id,title,created,shown,seen,source,category,resolved FROM notifications WHERE id=?',
                           (identity,)).fetchone()
        return self._items([row])[0] if row else None

    def list_page(self, page, *, page_size=4, anchor=None, pending_first=False):
        if (type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100 or
                (anchor is not None and (type(anchor) is not int or anchor < 0))):
            raise ValueError("invalid_notification_page")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            if anchor is None:
                anchor = db.execute("SELECT COALESCE(MAX(id),0) FROM notifications").fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM notifications WHERE id<=?", (anchor,)).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            if page > pages:
                raise ValueError("invalid_notification_page")
            rows = db.execute("""SELECT id,title,created,shown,seen,source,category,resolved FROM notifications
                WHERE id<=? ORDER BY CASE WHEN ? AND category='response' AND resolved IS NULL THEN 0 ELSE 1 END,
                         id DESC LIMIT ? OFFSET ?""", (anchor, pending_first, page_size, (page - 1) * page_size)).fetchall()
        return {"items": self._items(rows), "page": page, "pages": pages, "total": total, "anchor": anchor}

    @staticmethod
    def _session_key(source, session):
        scope = json.dumps([source, session], ensure_ascii=False).encode("utf-8")
        return hashlib.blake2s(scope, digest_size=7).digest()

    @staticmethod
    def _cache_id(identity, source, session, turn, created, *, session_id=False):
        event = json.dumps([source, session, turn, created], ensure_ascii=False).encode("utf-8")
        digest = hashlib.blake2s(event, digest_size=8).digest()
        if session_id:
            # Keep the numeric prefix readable by older firmware. The tagged
            # suffix carries the session key and an event-specific checksum.
            parts = (NotificationStore._session_key(source, session), digest[:4])
            suffix = "".join(base64.urlsafe_b64encode(part).decode("ascii").rstrip("=") for part in parts)
            return f"p-{identity:x}-S{suffix}"
        return f"p-{identity:x}-{digest.hex()}"

    def cache_records(self, *, pending_first=False, active_only=False, session_ids=False):
        with closing(self._connect()) as db:
            # Within one request show its latest unanswered question. A more
            # recent request supersedes older turns, including after answer.
            rows = db.execute(f"""SELECT id,title,created,shown,seen,source,category,resolved,session,turn
                FROM notifications AS n
                WHERE NOT ? OR (category='completed' AND id IN (
                    SELECT MAX(id) FROM notifications WHERE category='completed' AND seen IS NULL
                    GROUP BY source,session))
                    OR (category='response' AND resolved IS NULL AND dismissed IS NULL AND NOT ({SUPERSEDED_QUESTION}) AND NOT EXISTS (
                        SELECT 1 FROM notifications AS newer
                        WHERE newer.source=n.source AND newer.session=n.session
                          AND newer.category='response' AND newer.id>n.id
                          AND (newer.resolved IS NULL OR n.parent_turn IS NULL
                               OR newer.parent_turn IS NOT n.parent_turn)))
                ORDER BY CASE WHEN ? AND category='response' AND resolved IS NULL THEN 0 ELSE 1 END,
                         id DESC LIMIT 100""", (active_only, pending_first)).fetchall()
        records = self._items(rows)
        for record, row in zip(records, rows):
            # Showing a reminder on the device does not change its TF rendition.
            del record["shown"]
            completed = record["category"] == "completed"
            record["cache_id"] = self._cache_id(row[0], row[5], row[8], row[9], row[2],
                                                session_id=session_ids and completed)
            if completed:
                record["completion_session"] = row[8]
            if active_only:
                record["session"] = row[8]
        return records

    def _matching_cache_record(self, db, cache_id):
        parts = cache_id.split("-", 2)
        if len(parts) != 3 or parts[0] != "p" or not 1 <= len(parts[1]) <= 16:
            return None
        session_id = parts[2].startswith("S")
        try:
            identity = int(parts[1], 16)
        except ValueError:
            return None
        if not 0 < identity <= 0x7fff_ffff_ffff_ffff:
            return None
        event = db.execute("SELECT source,session,turn,created,category FROM notifications WHERE id=?",
                           (identity,)).fetchone()
        if (event and (not session_id or event[4] == "completed") and
                cache_id == self._cache_id(identity, *event[:4], session_id=session_id)):
            return identity
        return None

    def mark_cached_seen(self, cache_id, *, grouped_completions=False):
        with closing(self._connect()) as db, db:
            identity = self._matching_cache_record(db, cache_id)
            if identity is not None:
                if grouped_completions or cache_id.split("-", 2)[2].startswith("S"):
                    db.execute("""UPDATE notifications SET seen=COALESCE(seen,?) WHERE category='completed'
                        AND id<=? AND (source,session)=(SELECT source,session FROM notifications
                                                       WHERE id=? AND category='completed')""",
                               (time.time(), identity, identity))
                db.execute("UPDATE notifications SET seen=COALESCE(seen,?) WHERE id=?", (time.time(), identity))

    def dismiss_cached_response(self, cache_id):
        """Dismiss one board's reminder without asserting that it was answered."""
        with closing(self._connect()) as db, db:
            identity = self._matching_cache_record(db, cache_id)
            if identity is None:
                return False
            now = time.time()
            return db.execute("""UPDATE notifications SET dismissed=COALESCE(dismissed,?),
                seen=COALESCE(seen,?) WHERE id=? AND category='response'""",
                (now, now, identity)).rowcount > 0

    def mark_list_seen_through_cache_record(self, cache_id, *, include_responses=False):
        """Record that the list presented the inbox through its exact TF snapshot."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            identity = self._matching_cache_record(db, cache_id)
            if identity is not None:
                db.execute("""UPDATE notifications SET seen=? WHERE id<=? AND seen IS NULL
                              AND (? OR category='completed')""",
                           (time.time(), identity, include_responses))
            return identity

    def home_summary(self, *, include_receipts=False):
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            completed = db.execute("""SELECT source,COUNT(DISTINCT session),MAX(id) FROM notifications
                WHERE seen IS NULL AND category='completed' GROUP BY source ORDER BY MAX(id) DESC""").fetchall()
            response = db.execute(f"""SELECT n.source,groups.amount,n.id,n.title FROM notifications n JOIN
                (SELECT source,COUNT(*) amount,MAX(id) latest FROM notifications
                 AS n WHERE resolved IS NULL AND dismissed IS NULL AND category='response' AND NOT ({SUPERSEDED_QUESTION}) GROUP BY source) groups
                ON n.id=groups.latest ORDER BY n.id DESC""").fetchall()
            if include_receipts:
                receipts = [row[0] for row in db.execute(f"""SELECT id FROM notifications n WHERE shown IS NULL AND
                    ((category='completed' AND seen IS NULL) OR (category='response' AND resolved IS NULL AND dismissed IS NULL
                    AND NOT ({SUPERSEDED_QUESTION})))""")]
        summary = {"completed_count": sum(row[1] for row in completed),
                "completed_sources": [row[0] for row in completed],
                "response_source_count": len(response),
                "response_count": sum(row[1] for row in response),
                "response_sources": [{"source": row[0], "count": row[1], "id": row[2], "title": row[3]}
                                     for row in response]}
        return (summary, receipts) if include_receipts else summary

    def mark_home_shown(self, identities):
        with closing(self._connect()) as db, db:
            now = time.time()
            db.executemany("UPDATE notifications SET shown=COALESCE(shown,?) WHERE id=?",
                           ((now, identity) for identity in identities))

    def seen_notifications(self, identities, *, include_resolved=False):
        if not identities:
            return set()
        placeholders = ",".join("?" for _ in identities)
        with closing(self._connect()) as db:
            return {identity for (identity,) in db.execute(
                f"""SELECT id FROM notifications n WHERE id IN ({placeholders}) AND
                    (seen IS NOT NULL OR (? AND category='response' AND
                    (resolved IS NOT NULL OR ({SUPERSEDED_QUESTION}))))""",
                (*identities, include_resolved))}

    def cached_home_receipts(self, identities):
        if not identities:
            return (), 0
        placeholders = ",".join("?" for _ in identities)
        with closing(self._connect()) as db:
            rows = db.execute(f"""SELECT id,shown IS NULL,category='response' AND
                         id>COALESCE((SELECT through_id FROM notification_attention WHERE id=1),0)
                FROM notifications WHERE id IN ({placeholders}) AND
                  ((category='completed' AND seen IS NULL) OR (category='response' AND resolved IS NULL AND dismissed IS NULL))""",
                identities).fetchall()
        return tuple(identity for identity, shown_pending, _ in rows if shown_pending), max(
            (identity for identity, _, attention_pending in rows if attention_pending), default=0)

    def next_pending(self, *, text_only=False):
        # Shown legacy records have no action flag. Only an explicit retry keeps a shown record pending.
        with closing(self._connect()) as db:
            row = db.execute("""SELECT id,title,shown,action_pending,source,category FROM notifications
                WHERE seen IS NULL AND (category!='response' OR resolved IS NULL)
                  AND (shown IS NULL OR (? AND action_pending=1))
                ORDER BY id LIMIT 1""", (not text_only,)).fetchone()
        return {"id": row[0], "title": row[1], "shown": row[2] is not None, "action_pending": row[3],
                "source": row[4], "category": row[5]} if row else None

    def pending_attention(self):
        with closing(self._connect()) as db:
            return db.execute(f"""SELECT COALESCE(MAX(id),0) FROM notifications n
                WHERE category='response' AND resolved IS NULL AND dismissed IS NULL AND NOT ({SUPERSEDED_QUESTION})
                AND id>COALESCE((SELECT through_id FROM notification_attention WHERE id=1),0)""").fetchone()[0]

    def acknowledge_attention(self, identity):
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO notification_attention VALUES (1,?)
                ON CONFLICT(id) DO UPDATE SET through_id=MAX(through_id,excluded.through_id)""", (identity,))

    def set_action_pending(self, identity, pending):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE notifications SET action_pending=? WHERE id=?", (int(pending), identity))

    def latest_unseen(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT id,title,source,category FROM notifications WHERE seen IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return {"id": row[0], "title": row[1], "source": row[2], "category": row[3]} if row else None

    def mark_shown(self, identity):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE notifications SET shown=COALESCE(shown,?) WHERE id=?", (time.time(), identity))

    def mark_seen(self, identity):
        with closing(self._connect()) as db, db:
            return db.execute("UPDATE notifications SET seen=COALESCE(seen,?) WHERE id=?",
                              (time.time(), identity)).rowcount > 0

    def resolve_response(self, identity):
        """The adapter calls this only after its source reports a request was answered."""
        with closing(self._connect()) as db, db:
            return db.execute("UPDATE notifications SET resolved=? WHERE id=? AND category='response' AND resolved IS NULL",
                              (time.time(), identity)).rowcount > 0

    def resolve_agent_event(self, source, session, event_id):
        """Remember a response even if its opening event is delivered later."""
        return self.resolve_agent_events(source, session, (event_id,))

    def resolve_agent_events(self, source, session, event_ids, *, remember_missing=True):
        """Close a tool's settled requests in a single inbox update."""
        with closing(self._connect()) as db, db:
            now = time.time()
            events = [(source, session, event_id, now) for event_id in event_ids]
            if remember_missing:
                db.executemany("INSERT OR IGNORE INTO notification_resolutions VALUES (?,?,?,?)", events)
            return db.executemany("""UPDATE notifications SET resolved=? WHERE source=? AND session=? AND turn=?
                AND category='response' AND resolved IS NULL""",
                                  ((now, source, session, event_id) for source, session, event_id, now in events)).rowcount

    def resolve_session_requests(self, source, session):
        """Settle current requests without affecting another turn in this session."""
        with closing(self._connect()) as db, db:
            return db.execute("""UPDATE notifications SET resolved=? WHERE source=? AND session=?
                AND category='response' AND resolved IS NULL""", (time.time(), source, session)).rowcount

    def mark_seen_many(self, identities):
        with closing(self._connect()) as db, db:
            now = time.time()
            db.executemany("UPDATE notifications SET seen=COALESCE(seen,?) WHERE id=?",
                           ((now, identity) for identity in identities))

    def unseen_count(self):
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM notifications WHERE seen IS NULL").fetchone()[0]

    def has_active_notifications(self):
        with closing(self._connect()) as db:
            return db.execute(f"""SELECT 1 FROM notifications n WHERE
                (category='response' AND resolved IS NULL AND dismissed IS NULL AND NOT ({SUPERSEDED_QUESTION})) OR
                (category='completed' AND seen IS NULL) LIMIT 1""").fetchone() is not None


async def deliver_next(client, store):
    item = store.next_pending()
    if item is None:
        return False
    pending = False
    if item["action_pending"] != 0 and item["category"] == "completed":
        if client.channel.capabilities.get("role_actions") == 1:
            state = await client.status()
            action = state.get("finish_action")
            pending = state.get("role_state") == "loading"
            if state.get("role_state") == "ready" and action:
                actions = await client.list_actions()
                if action in actions:
                    result = await client.play_role(actions.index(action), 3000)
                    pending = result.get("action_status") in ("busy", "expired")
        store.set_action_pending(item["id"], pending)
    elif item["category"] == "response" and item["action_pending"] is None:
        store.set_action_pending(item["id"], False)
    if item["shown"]:
        item = store.next_pending(text_only=True)
        if item is None:
            return not pending
        # Keep an untried completion action eligible; a response request has
        # no completion action and must not create a retry loop.
        if item["action_pending"] is None:
            store.set_action_pending(item["id"], item["category"] == "completed")
    try:
        try:
            receipt = await client.show_text(notification_text(item))
        except ValueError:
            receipt = await client.show_text(notification_text(item, detail=False))
    except TextNotVisible:
        return False
    if receipt.get("status") not in ("shown", "finished"):
        return False
    store.mark_shown(item["id"])
    return not pending


def request_worker_yield(config_path):
    root = Path(config_path).parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(root / "notification-worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            acquire(descriptor, blocking=False)
        except BlockingIOError:
            signal = os.open(root / "notification-handoff", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(signal)
            return True
        return False
    finally:
        os.close(descriptor)


async def run_worker(config_path, *, client_factory=None, max_seconds=86400, poll_interval=0.5,
                     handoff_delay=7, idle_seconds=15):
    return await _run_workers(config_path,client_factory=client_factory,max_seconds=max_seconds,
                              poll_interval=poll_interval,handoff_delay=handoff_delay,idle_seconds=idle_seconds,
                              receivers_only=False)


async def run_receiver_worker(config_path, *, client_factory=None, max_seconds=86400, poll_interval=0.5,
                              handoff_delay=7, idle_seconds=15):
    return await _run_workers(config_path,client_factory=client_factory,max_seconds=max_seconds,
                              poll_interval=poll_interval,handoff_delay=handoff_delay,idle_seconds=idle_seconds,
                              receivers_only=True)


async def run_live_receiver(config_path, shared_config, *, client_factory=None, token_probe=None, max_seconds=None,
                            poll_interval=0.5, handoff_delay=7):
    """Keep one registered receiver's data and notifications up to date."""
    from .agent_speed_probe import find_installed_probe
    from .device_registry import DeviceRegistry
    from .quota_capture import QuotaStore
    config_path, shared_config = Path(config_path), Path(shared_config)
    if config_path.resolve() not in {path.resolve() for path in DeviceRegistry(shared_config).receiver_paths()}:
        raise ValueError('设备不是已登记的独立接收端。')
    if token_probe is None:
        probe = find_installed_probe(shared_config)
    else:
        probe = Path(token_probe).expanduser()
        if not probe.is_file():
            raise ValueError('找不到指定的速度采集程序。')
    deadline=float('inf') if max_seconds is None else time.monotonic()+max_seconds
    # Announce ownership for the entire service lifetime, including while it
    # waits for an existing notification-only receiver to release the board.
    # A lock, unlike a marker file, cannot remain stale after a crash.
    intent = os.open(config_path.parent / 'live-receiver.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while time.monotonic() < deadline:
            try:
                acquire(intent, blocking=False)
                break
            except BlockingIOError:
                await asyncio.sleep(1)
        else:
            return
        await _run_device_worker(config_path, source=NotificationStore(shared_config.parent / 'notifications.sqlite3'),
                                 client_factory=client_factory, deadline=deadline,
                                 poll_interval=poll_interval, handoff_delay=handoff_delay,
                                 live_options={'token_probe': probe,
                                               'quota_store': QuotaStore(shared_config.parent / 'agent-quota.sqlite3')})
    finally:
        os.close(intent)


def _live_receiver_requested(config_path):
    descriptor = os.open(config_path.parent / 'live-receiver.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            acquire(descriptor, blocking=False)
        except BlockingIOError:
            return True
        release(descriptor)
        return False
    finally:
        os.close(descriptor)


async def _run_workers(config_path, *, client_factory, max_seconds, poll_interval, handoff_delay, idle_seconds, receivers_only):
    from .device_registry import DeviceRegistry

    registry = DeviceRegistry(config_path)
    workers = {}
    root = registry.default_path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox = NotificationStore(root / "notifications.sqlite3")
    deadline = time.monotonic() + max_seconds
    next_scan = 0.0
    idle_since = None
    dispatch_name="receiver-dispatch.lock" if receivers_only else "notification-dispatch.lock"
    descriptor = os.open(root / dispatch_name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            acquire(descriptor, blocking=False)
        except BlockingIOError:
            return
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_scan:
                paths = await asyncio.to_thread(registry.receiver_paths if receivers_only else registry.paths)
                next_scan = now + 5
                for path in paths:
                    if path not in workers:
                        source = None if path == registry.default_path else inbox
                        store = inbox if source is None else NotificationStore(path.parent / "notifications.sqlite3", source=source)
                        workers[path] = store, asyncio.create_task(_run_device_worker(
                            path, source=source, client_factory=client_factory, deadline=deadline,
                            poll_interval=poll_interval, handoff_delay=handoff_delay))
            if not workers or all(task.done() for _, task in workers.values()):
                return
            idle_due = idle_since is not None and now - idle_since >= idle_seconds
            # Take the version before syncing the per-board stores, so a
            # committed event after those checks is visible at handoff.
            through = await asyncio.to_thread(_inbox_version, inbox) if idle_due else None
            active = await asyncio.gather(*[asyncio.to_thread(_sync_active, store) for store, _ in workers.values()])
            if any(active):
                idle_since = None
            elif idle_since is None:
                idle_since = now
            elif through is not None and _release_idle_dispatch(inbox, descriptor, through):
                return
            await asyncio.sleep(poll_interval)
    finally:
        tasks = [task for _, task in workers.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        os.close(descriptor)


_INBOX_VERSION = "SELECT COALESCE(MAX(id),0) FROM notifications"


def _inbox_version(inbox):
    try:
        with closing(inbox._connect()) as db:
            return db.execute(_INBOX_VERSION).fetchone()[0]
    except (OSError, sqlite3.Error, ValueError):
        return None


def _release_idle_dispatch(inbox, descriptor, through):
    try:
        with closing(inbox._connect(timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(_INBOX_VERSION).fetchone()[0] != through:
                return False
            # The next hook starts its worker after committing. Release the
            # dispatch lock before that commit is possible. The per-board
            # locks remain held until each old connection has closed.
            release(descriptor)
            return True
    except (OSError, sqlite3.Error, ValueError):
        return False


def _sync_active(store):
    try:
        store.sync()
        return store.has_active_notifications()
    except (OSError, sqlite3.Error, ValueError):
        return True


async def _run_device_worker(config_path, *, source, client_factory, deadline, poll_interval,
                             handoff_delay, live_options=None):
    from .client import Client
    from .mcp_server import BoardSession
    from .pairing import DeviceConfig

    client_factory = client_factory or Client
    config_path = Path(config_path)
    try:
        DeviceConfig.load(config_path)
    except (FileNotFoundError, OSError, ValueError):
        return
    lock_path = config_path.parent / "notification-worker.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    tasks = []
    session = None
    def start_session():
        board = BoardSession(config_path, background=True, client_factory=client_factory,
                             notification_source=source, **(live_options or {}))
        methods = [board.poll]
        if live_options is not None:
            methods.extend((board.poll_weather, board.poll_tokens, board.poll_quota,
                            board.poll_quota_display, board.poll_computer_metrics))
        return board, [asyncio.create_task(method()) for method in methods]

    async def stop_session():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if session is not None:
            await session.drop()

    try:
        while time.monotonic() < deadline:
            if source is not None and live_options is None and _live_receiver_requested(config_path):
                return
            try:
                acquire(descriptor, blocking=False)
                break
            except BlockingIOError:
                await asyncio.sleep(1)
        else:
            return
        handoff = config_path.parent / "notification-handoff"
        session, tasks = start_session()
        while time.monotonic() < deadline:
            if source is not None and live_options is None and _live_receiver_requested(config_path):
                return
            if live_options is not None:
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError('独立设备的数据更新意外停止。')
            if handoff.exists():
                handoff.unlink(missing_ok=True)
                await stop_session()
                session, tasks = None, []
                await asyncio.sleep(handoff_delay)
                session, tasks = start_session()
            await asyncio.sleep(poll_interval)
    finally:
        await stop_session()
        os.close(descriptor)
