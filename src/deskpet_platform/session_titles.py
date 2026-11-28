"""Read session titles from the Agent's own local records."""
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID


def _select(path, query, identity):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            return db.execute(query, (identity,)).fetchone() or ()
    except (OSError, sqlite3.Error):
        return ()


def _log_title(root, identity):
    for path in root.glob("*/*.jsonl"):
        if path.stem != identity:
            continue
        try:
            with path.open("rb") as stream:
                offset = max(0, path.stat().st_size - 65536)
                stream.seek(offset)
                if offset:
                    stream.readline()
                for line in reversed(stream.read(65536).splitlines()):
                    try:
                        event = json.loads(line)
                    except (UnicodeError, ValueError):
                        continue
                    if isinstance(event, dict) and event.get("type") in ("custom-title", "ai-title"):
                        title = event.get("customTitle") or event.get("aiTitle")
                        if isinstance(title, str) and title.strip():
                            return title
        except OSError:
            pass
    return None


def _object(value):
    return value if isinstance(value, dict) else {}


def _record(line):
    try:
        return _object(json.loads(line))
    except (UnicodeError, ValueError, RecursionError):
        return {}


def _title(*values):
    return next((value for value in values if isinstance(value, str) and value.strip()), None)


def _codex_thread_id(identity):
    try:
        return str(UUID(identity[-36:])) if isinstance(identity, str) else None
    except ValueError:
        return None


def _codex_index_title(root, thread):
    title = None
    try:
        with (root / "session_index.jsonl").open("rb") as stream:
            for line in stream:
                entry = _record(line)
                if entry.get("id") == thread:
                    title = _title(entry.get("thread_name"), title)
    except OSError:
        pass
    return title


def _codex_rollout(roots, identity, thread):
    basename = Path(identity).name
    if basename.startswith("rollout-"):
        try:
            day = date.fromisoformat(basename[8:18])
        except ValueError:
            pass
        else:
            for root in roots:
                path = root / "sessions" / f"{day:%Y/%m/%d}" / (basename + ".jsonl")
                if path.is_file():
                    return path
    for root in roots:
        for directory in ("sessions", "archived_sessions"):
            path = next((root / directory).rglob(f"*{thread}.jsonl"), None)
            if path is not None:
                return path
    return None


def _codex_rollout_details(path, thread):
    # Title changes can appear near the start or after a long run of tool output.
    window = 128 * 1024
    try:
        with path.open("rb") as stream:
            record = _record(stream.readline(window))
            meta = _object(record.get("payload"))
            metadata_id = _codex_thread_id(meta.get("id"))
            history_id = _codex_thread_id(_object(meta.get("history_base")).get("thread_id"))
            # The probe reports the exact rollout filename. The header may use a different ID.
            if record.get("type") != "session_meta" or not metadata_id or not path.stem.endswith(thread):
                return {}, None
            head = stream.read(window)
            end = stream.seek(0, os.SEEK_END)
            tail_start = max(0, end - window)
            stream.seek(tail_start)
            tail = stream.read(window)
    except OSError:
        return {}, None
    for line in reversed(head.splitlines() + tail.splitlines()):
        record = _record(line)
        event = _object(record.get("payload"))
        if record.get("type") == "event_msg" and event.get("type") == "thread_name_updated":
            if title := _title(event.get("thread_name")):
                return meta, title
    base_header = metadata_id != thread and metadata_id == history_id
    return meta, None if base_header else _title(meta.get("thread_name"), meta.get("title"))


def _codex_store_title(roots, thread):
    for root in roots:
        if title := _title(*_select(root / "state_5.sqlite", "SELECT name,title FROM threads WHERE id=?", thread)):
            return title
        if title := _codex_index_title(root, thread):
            return title
    return None


def _codex_title(home, identity):
    primary = Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser()
    cockpit = home / ".antigravity_cockpit/instances/codex"
    roots = tuple(dict.fromkeys((primary, *sorted(cockpit.glob("*")))))
    trail = set()
    label = None
    nickname = None
    while (thread := _codex_thread_id(identity)) and thread not in trail and len(trail) < 8:
        trail.add(thread)
        if title := _codex_store_title(roots, thread):
            return f"{label} · {title}" if label else title
        path = _codex_rollout(roots, identity, thread)
        if path is None:
            break
        meta, title = _codex_rollout_details(path, thread)
        if title:
            return f"{label} · {title}" if label else title
        subagent = _object(_object(meta.get("source")).get("subagent"))
        spawn = _object(subagent.get("thread_spawn"))
        branch = _object(meta.get("history_base")).get("thread_id")
        metadata_id = _codex_thread_id(meta.get("id"))
        if metadata_id and metadata_id not in (thread, _codex_thread_id(branch)):
            if title := _codex_store_title(roots, metadata_id):
                return f"{label} · {title}" if label else title
        parent = _title(meta.get("parent_thread_id"), spawn.get("parent_thread_id"), branch)
        if label is None:
            nickname = _title(meta.get("agent_nickname"), spawn.get("agent_nickname"))
            label = nickname or ("子会话" if subagent else "分支")
        if not parent:
            break
        identity = parent
    return nickname


def lookup_session_title(source, identity):
    home = Path.home()
    identity = identity.removesuffix(".jsonl").removesuffix(".json").removesuffix(".log")
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share").expanduser()
    if source == "codex":
        return _codex_title(home, identity)
    elif source == "devin":
        values = _select(data_home / "devin/cli/sessions.db", "SELECT title FROM sessions WHERE id=?",
                         identity.removeprefix("sessions.db#"))
    elif source == "opencode":
        values = _select(data_home / "opencode/opencode.db", "SELECT title FROM session WHERE id=?", identity)
    elif source == "claudeCode":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
        return _log_title(root / "projects", identity)
    elif source in ("workbuddy", "workbuddyAi"):
        return _log_title(home / (".workbuddy" if source == "workbuddy" else ".workbuddy-ai") / "projects", identity)
    elif source == "antigravity":
        from .antigravity_feedback import _summary_title
        return _summary_title(identity, None)
    else:
        return None
    return _title(*values)
