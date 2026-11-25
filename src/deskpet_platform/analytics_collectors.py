"""Read usage metadata from local Agent logs without retaining message bodies."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import sqlite3
from contextlib import closing

# Exact model identifiers observed in Antigravity logs. An unclassified reply
# cannot be attributed to either provider's allowance bucket.
ANTIGRAVITY_MODELS = {
    "gemini-4-argon-high": 2,
    "gemini-4-argon-medium": 2,
    "gemini-4-neon-xhigh": 2,
    "gemini-4-hydrogen-tiered-high": 2,
    "gemini-4-hydrogen-xhigh": 2,
    "claude-opus-5-5-high": 3,
}

MAX_AMOUNT = (1 << 53) - 1


@dataclass(frozen=True, slots=True)
class Event:
    source: int
    session: str
    key: str
    timestamp: float
    amount: int | None = None
    turn: bool = False
    title: str = ""
    running: bool | None = None
    codex_total: int | None = None
    codex_legacy: bool = False


def _dict(value):
    return value if isinstance(value, dict) else {}


def _id(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable() else ""


def _title(value):
    if not isinstance(value, str):
        return ""
    clean = " ".join("".join(c for c in value[:512] if c.isprintable() or c.isspace()).split())
    return clean.encode("utf-8", errors="ignore")[:47].decode("utf-8", errors="ignore")


def _amount(value):
    return value if type(value) is int and 0 <= value <= MAX_AMOUNT else None


def _time(value):
    try:
        if type(value) in (int, float):
            stamp = value / 1000 if value >= 10**11 else value
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            stamp = dt.timestamp()
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return stamp if 1577836800 <= stamp <= 4102444799 else None


def _codex(row, session, position, stamp):
    payload = _dict(row.get("payload"))
    kind = row.get("type")
    if kind == "session_meta":
        session = _id(payload.get("id")) or session
        return session, None
    if stamp is None:
        return session, None
    if kind == "token_usage_record":
        response = _id(payload.get("response_id"))
        amount = _amount(_dict(payload.get("usage")).get("output_tokens"))
        if amount is not None:
            total = _amount(_dict(payload.get("thread_token_usage")).get("output_tokens"))
            return session, Event(0, session, "response:" + response if response else f"usage:{stamp}:{position}",
                                  stamp, amount=amount, codex_total=total)
    if kind == "event_msg":
        kind = payload.get("type")
        if kind == "token_count":
            amount = _amount(_dict(_dict(payload.get("info")).get("last_token_usage")).get("output_tokens"))
            if amount is not None:
                total = _amount(_dict(_dict(payload.get("info")).get("total_token_usage")).get("output_tokens"))
                return session, Event(0, session, f"legacy:{stamp}:{position}", stamp, amount=amount,
                                      codex_total=total, codex_legacy=True)
        turn = _id(payload.get("turn_id"))
        if turn and kind in ("task_started", "task_complete", "turn_aborted"):
            return session, Event(0, session, f"{kind}:{turn}", stamp, turn=kind == "task_started",
                                  running=kind == "task_started")
        if kind == "thread_name_updated":
            return session, Event(0, session, f"title:{stamp}", stamp, title=_title(payload.get("thread_name")))
    return session, None


def _claude(row, session, position, stamp):
    if row.get("isSidechain") is True:
        return session, None
    session = _id(row.get("sessionId")) or session
    if stamp is None:
        return session, None
    kind = row.get("type")
    message = _dict(row.get("message"))
    identity = _id(row.get("uuid")) or str(position)
    if kind == "assistant":
        identity = _id(message.get("id")) or identity
        usage = _amount(_dict(message.get("usage")).get("output_tokens"))
        return session, Event(1, session, "assistant:" + identity, stamp, amount=usage)
    if kind == "user" and not row.get("isMeta") and not row.get("toolUseResult"):
        content = message.get("content")
        if isinstance(content, str) or (isinstance(content, list) and
                                       any(_dict(part).get("type") == "text" for part in content)):
            return session, Event(1, session, "user:" + (_id(row.get("promptId")) or identity), stamp, turn=True)
    if kind == "custom-title":
        return session, Event(1, session, "title:" + identity, stamp, title=_title(row.get("customTitle")))
    return session, None


def _antigravity(row, session, position, stamp):
    index = row.get("step_index")
    if stamp is None or type(index) is not int or index < 0:
        return session, None
    kind, origin = row.get("type"), row.get("source")
    if kind == "USER_INPUT" and origin == "USER_EXPLICIT":
        return session, Event(-1, session, f"prompt:{index}", stamp, turn=True)
    if kind == "PLANNER_RESPONSE" and origin == "MODEL":
        model = row.get("model")
        source = ANTIGRAVITY_MODELS.get(model, -1) if isinstance(model, str) else -1
        return session, Event(source, session, f"planner:{index}", stamp, amount=_amount(row.get("output_tokens")))
    return session, None


def _workbuddy(row, session, position, stamp):
    data = _dict(row.get("providerData"))
    if data.get("isMeta") or data.get("discard"):
        return session, None
    session = _id(row.get("sessionId")) or session
    if stamp is None:
        return session, None
    kind, role = row.get("type"), row.get("role")
    identity = _id(row.get("id")) or f"{stamp}:{position}"
    if kind in ("ai-title", "custom-title"):
        title = _title(row.get("customTitle") if kind == "custom-title" else row.get("aiTitle"))
        return session, Event(5, session, f"title:{identity}", stamp, title=title)
    if kind == "message" and role == "user" and data.get("startsNewUserRequest") is not False:
        key = _id(data.get("conversationRequestId")) or identity
        return session, Event(5, session, "user:" + key, stamp, turn=True)
    if (kind == "message" and role == "assistant") or kind == "function_call":
        usage = _amount(_dict(_dict(row.get("message")).get("usage")).get("output_tokens"))
        if usage is not None:
            key = _id(data.get("messageId")) or identity
            return session, Event(5, session, "assistant:" + key, stamp, amount=usage)
    return session, None


def jsonl_events(kind, stream, session):
    """Yield complete-line cursor positions alongside normalized events."""
    parser = {"codex": _codex, "claude": _claude, "antigravity": _antigravity, "workbuddy": _workbuddy}[kind]
    while line := stream.readline():
        if not line.endswith(b"\n"):
            break
        position = stream.tell()
        try:
            row = _dict(json.loads(line))
        except (UnicodeError, ValueError, RecursionError):
            row = {}
        stamp = _time(row.get("created_at") if kind == "antigravity" else row.get("timestamp"))
        session, event = parser(row, session, position, stamp)
        yield position, session, event


def log_paths(home):
    codex_root = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
    # A still-active conversation can have been created before the reporting window.
    for path in codex_root.glob("*/*/*/*.jsonl"):
        yield "codex", path, path.stem
    claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")) / "projects"
    for path in claude_root.glob("*/*.jsonl"):
        yield "claude", path, path.stem
    for path in (home / ".gemini/antigravity/brain").glob("*/.system_generated/logs/transcript.jsonl"):
        yield "antigravity", path, path.parents[2].name
    for edition in (".workbuddy", ".workbuddy-ai"):
        for path in (home / edition / "projects").glob("*/*.jsonl"):
            yield "workbuddy", path, path.stem


def opencode_events(path, earliest):
    """Read changed message rows from the app's SQLite database."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
        rows = db.execute("""
            SELECT m.id,m.session_id,m.time_created,m.data,s.title
            FROM message m JOIN session s ON s.id=m.session_id
            WHERE s.parent_id IS NULL AND s.time_updated >= ?
            ORDER BY m.time_created,m.id
        """, (int(earliest * 1000),))
        for identity, session, stamp, raw, title in rows:
            try:
                message = _dict(json.loads(raw))
            except (TypeError, UnicodeError, ValueError, RecursionError):
                continue
            stamp = _time(stamp)
            if stamp is None or not _id(session) or not _id(identity):
                continue
            role = message.get("role")
            if role == "assistant":
                amount = _amount(_dict(message.get("tokens")).get("output"))
                yield Event(4, session, identity, stamp, amount=amount)
            elif role == "user":
                yield Event(4, session, identity, stamp, turn=True)
            if title:
                yield Event(4, session, "title", stamp, title=_title(title))
