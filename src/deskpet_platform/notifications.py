"""Local notification records; never update Codex's own read state."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import fcntl
import sqlite3
import time
import asyncio


SOURCE_NAMES = {"codex": "Codex", "claude_code": "Claude Code", "workbuddy": "WorkBuddy",
                "antigravity": "Antigravity", "opencode": "OpenCode"}


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
        self.path = Path(path)
        self.source = source

    def _connect(self, *, timeout=2):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
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
            turn_schema = "SELECT 1 FROM pragma_table_info('notifications') WHERE name='parent_turn'"
            if db.execute(turn_schema).fetchone() is None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute(turn_schema).fetchone() is None:
                        db.execute("ALTER TABLE notifications ADD COLUMN parent_turn TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_resolutions (
                source TEXT NOT NULL, session TEXT NOT NULL, event_id TEXT NOT NULL, resolved REAL NOT NULL,
                PRIMARY KEY(source,session,event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_attention (
                id INTEGER PRIMARY KEY CHECK(id=1), through_id INTEGER NOT NULL)""")
        except sqlite3.Error:
            db.close()
            raise
        return db

    def sync(self):
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
                db.execute("""INSERT OR IGNORE INTO main.notifications
                    (source,session,turn,title,created,category,parent_turn,resolved)
                    SELECT source,session,turn,title,created,category,parent_turn,resolved
                    FROM inbox.notifications WHERE id>? ORDER BY id""", (after,))
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
            pending = db.execute("""SELECT 1 FROM notifications WHERE source='codex' AND session=?
                AND parent_turn=? AND category='response' AND resolved IS NULL LIMIT 1""", (session, turn)).fetchone()
            if pending:
                return None
            return self._insert(db, "codex", session, turn, "completed", title, None)

    def add_agent_event(self, source, session, event_id, category, title, *, parent_turn=None):
        """Record an explicit adapter event, idempotently within one agent session."""
        valid_source = (isinstance(source, str) and 0 < len(source) <= 32 and
                        all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in source))
        valid_id = lambda value: isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable()
        if (not valid_source or not valid_id(session) or not valid_id(event_id) or
                category not in ("completed", "response") or
                (parent_turn is not None and not valid_id(parent_turn)) or
                not isinstance(title, str) or not title.strip() or not title.isprintable()):
            raise ValueError("invalid_agent_event")
        title = " ".join(title.split())[:64]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._insert(db, source, session, event_id, category, title, parent_turn)

    @staticmethod
    def _insert(db, source, session, event_id, category, title, parent_turn):
        resolution = db.execute("SELECT resolved FROM notification_resolutions WHERE source=? AND session=? AND event_id=?",
                                (source, session, event_id)).fetchone() if category == "response" else None
        db.execute("""INSERT OR IGNORE INTO notifications
            (source,session,turn,title,created,category,parent_turn,resolved) VALUES (?,?,?,?,?,?,?,?)""",
                   (source, session, event_id, title, time.time(), category, parent_turn, resolution[0] if resolution else None))
        identity, stored_category, stored_turn = db.execute(
            "SELECT id,category,parent_turn FROM notifications WHERE source=? AND session=? AND turn=?",
            (source, session, event_id)).fetchone()
        if stored_category != category or stored_turn != parent_turn:
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

    def list_page(self, page, *, page_size=4, anchor=None):
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
                WHERE id<=? ORDER BY id DESC LIMIT ? OFFSET ?""", (anchor, page_size, (page - 1) * page_size)).fetchall()
        return {"items": self._items(rows), "page": page, "pages": pages, "total": total, "anchor": anchor}

    @staticmethod
    def _cache_id(identity, source, session, turn, created):
        event = json.dumps([source, session, turn, created], ensure_ascii=False).encode("utf-8")
        return f"p-{identity:x}-{hashlib.blake2s(event, digest_size=8).hexdigest()}"

    def cache_records(self):
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT id,title,created,shown,seen,source,category,resolved,session,turn
                FROM notifications ORDER BY id DESC LIMIT 100""").fetchall()
        records = self._items(rows)
        for record, row in zip(records, rows):
            record["cache_id"] = self._cache_id(row[0], row[5], row[8], row[9], row[2])
        return records

    def mark_cached_seen(self, cache_id):
        parts = cache_id.split("-")
        if len(parts) != 3 or parts[0] != "p" or not 1 <= len(parts[1]) <= 16:
            return
        try:
            identity = int(parts[1], 16)
        except ValueError:
            return
        if not 0 < identity <= 0x7fff_ffff_ffff_ffff:
            return
        with closing(self._connect()) as db, db:
            event = db.execute("SELECT source,session,turn,created FROM notifications WHERE id=?", (identity,)).fetchone()
            if event and cache_id == self._cache_id(identity, *event):
                db.execute("UPDATE notifications SET seen=COALESCE(seen,?) WHERE id=?", (time.time(), identity))

    def home_summary(self, *, include_receipts=False):
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            completed = db.execute("""SELECT source,COUNT(*),MAX(id) FROM notifications
                WHERE seen IS NULL AND category='completed' GROUP BY source ORDER BY MAX(id) DESC""").fetchall()
            response = db.execute("""SELECT n.source,groups.amount,n.id,n.title FROM notifications n JOIN
                (SELECT source,COUNT(*) amount,MAX(id) latest FROM notifications
                 WHERE resolved IS NULL AND category='response' GROUP BY source) groups
                ON n.id=groups.latest ORDER BY n.id DESC""").fetchall()
            if include_receipts:
                receipts = [row[0] for row in db.execute("""SELECT id FROM notifications WHERE shown IS NULL AND
                    ((category='completed' AND seen IS NULL) OR (category='response' AND resolved IS NULL))""")]
        summary = {"completed_count": sum(row[1] for row in completed),
                "completed_sources": [row[0] for row in completed],
                "response_source_count": len(response),
                "response_count": sum(row[1] for row in response),
                "response_sources": [{"source": row[0], "count": row[1], "id": row[2], "title": row[3]}
                                     for row in response[:3]]}
        return (summary, receipts) if include_receipts else summary

    def mark_home_shown(self, identities):
        with closing(self._connect()) as db, db:
            now = time.time()
            db.executemany("UPDATE notifications SET shown=COALESCE(shown,?) WHERE id=?",
                           ((now, identity) for identity in identities))

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
            return db.execute("""SELECT COALESCE(MAX(id),0) FROM notifications
                WHERE category='response' AND resolved IS NULL
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

    def resolve_agent_events(self, source, session, event_ids):
        """Close a tool's settled requests in a single inbox update."""
        with closing(self._connect()) as db, db:
            now = time.time()
            events = [(source, session, event_id, now) for event_id in event_ids]
            db.executemany("INSERT OR IGNORE INTO notification_resolutions VALUES (?,?,?,?)", events)
            return db.executemany("""UPDATE notifications SET resolved=? WHERE source=? AND session=? AND turn=?
                AND category='response' AND resolved IS NULL""",
                                  ((now, source, session, event_id) for source, session, event_id, now in events)).rowcount

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
            return db.execute("""SELECT 1 FROM notifications WHERE
                (category='response' AND resolved IS NULL) OR
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
        receipt = await client.show_text(notification_text(item))
    except ValueError:
        receipt = await client.show_text(notification_text(item, detail=False))
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
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            signal = os.open(root / "notification-handoff", os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(signal)
            return True
        return False
    finally:
        os.close(descriptor)


async def run_worker(config_path, *, client_factory=None, max_seconds=86400, poll_interval=0.5,
                     handoff_delay=7, idle_seconds=15):
    from .device_registry import DeviceRegistry

    registry = DeviceRegistry(config_path)
    workers = {}
    root = registry.default_path.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox = NotificationStore(root / "notifications.sqlite3")
    deadline = time.monotonic() + max_seconds
    next_scan = 0.0
    idle_since = None
    descriptor = os.open(root / "notification-dispatch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_scan:
                paths = await asyncio.to_thread(registry.paths)
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
            fcntl.flock(descriptor, fcntl.LOCK_UN)
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
                             handoff_delay):
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
    task = None
    session = None
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(1)
        else:
            return
        handoff = config_path.parent / "notification-handoff"
        session = BoardSession(config_path, background=True, client_factory=client_factory, notification_source=source)
        task = asyncio.create_task(session.poll())
        while time.monotonic() < deadline:
            if handoff.exists():
                handoff.unlink(missing_ok=True)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await session.drop()
                await asyncio.sleep(handoff_delay)
                session = BoardSession(config_path, background=True, client_factory=client_factory, notification_source=source)
                task = asyncio.create_task(session.poll())
            await asyncio.sleep(poll_interval)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session is not None:
            await session.drop()
        os.close(descriptor)
