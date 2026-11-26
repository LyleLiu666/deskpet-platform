"""Read usage metadata from local Agent logs without retaining message bodies."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import re
import sqlite3
from contextlib import closing

# Exact model identifiers observed in Antigravity logs. An unclassified reply
# cannot be attributed to either provider's allowance bucket.
ANTIGRAVITY_MODELS = {
    "gemini-3.8-flash-tiered": 2,
    "gemini-4-argon": 2,
    "gemini-4-argon-high": 2,
    "gemini-4-argon-medium": 2,
    "gemini-4-argon-trial": 2,
    "gemini-4-carbon": 2,
    "gemini-4-carbon-high": 2,
    "gemini-4-neon": 2,
    "gemini-4-neon-xhigh": 2,
    "gemini-4-hydrogen": 2,
    "gemini-4-hydrogen-medium": 2,
    "gemini-4-hydrogen-tiered-high": 2,
    "gemini-4-hydrogen-xhigh": 2,
    "claude-opus-5-5-high": 3,
    "claude-opus-5-5-medium": 3,
}

MAX_AMOUNT = (1 << 53) - 1
CODEX_ROLLOUT_NAME = re.compile(
    r"rollout-(\d{4})-(\d{2})-(\d{2})T\d{2}-\d{2}-\d{2}-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl"
)


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
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    codex_compatibility: bool = False


@dataclass(slots=True)
class CodexRollout:
    metadata_seen: bool = False
    fork_prefix_pending: bool = False
    pending_response: str = ""
    previous_total: tuple[int | None, ...] | None = None


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


def _included_input(total, cached):
    total, cached = _amount(total), _amount(cached)
    return {
        "input_tokens": total,
        "cached_input_tokens": cached if total is not None and cached is not None and cached <= total else None,
    }


def _split_input(uncached, cached, written):
    parts = tuple(map(_amount, (uncached, cached, written)))
    return _included_input(sum(parts) if None not in parts else None, cached)


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


def _codex_usage(raw):
    raw = _dict(raw)
    cache = raw.get("cached_input_tokens")
    if cache is None:
        cache = raw.get("cache_read_input_tokens")
    return tuple(map(_amount, (raw.get("output_tokens"), raw.get("input_tokens"), cache)))


def _codex(row, session, position, stamp, rollout):
    payload = _dict(row.get("payload"))
    kind = row.get("type")
    if kind == "session_meta":
        if not rollout.metadata_seen and (identity := _id(payload.get("id"))):
            session, rollout.metadata_seen = identity, True
        return session, None
    if stamp is None:
        return session, None
    if kind == "token_usage_record":
        response = _id(payload.get("response_id"))
        amount, inputs, cached = _codex_usage(payload.get("usage"))
        key = "response:" + response if response else f"usage:{stamp}:{position}"
        # A structured response report is followed by its token_count report in
        # the same turn. Their cumulative counters may have different baselines.
        thread = _id(payload.get("thread_id"))
        rollout.pending_response = ""
        if amount is not None:
            if response and thread == session and _id(payload.get("turn_id")):
                rollout.pending_response = key
            total = _amount(_dict(payload.get("thread_token_usage")).get("output_tokens"))
            return session, Event(0, session, key,
                                  stamp, amount=amount, codex_total=total,
                                  **_included_input(inputs, cached))
    if kind == "event_msg":
        kind = payload.get("type")
        if kind == "token_count":
            info = _dict(payload.get("info"))
            if not info:
                return session, None
            pending, rollout.pending_response = rollout.pending_response, ""
            previous = rollout.previous_total
            totals = _codex_usage(info.get("total_token_usage"))
            if any(value is not None for value in totals):
                rollout.previous_total = totals
            last = info.get("last_token_usage")
            usage = _codex_usage(last)
            if not isinstance(last, dict) and totals[0] is not None:
                baseline = previous if previous is not None else (0, 0, 0)
                usage = tuple(max(value-before, 0) if value is not None and before is not None else None
                              for value, before in zip(totals, baseline))
                if not any(usage):
                    return session, None
            amount, inputs, cached = usage
            if amount is not None:
                input_usage = _included_input(inputs, cached)
                complete = input_usage["cached_input_tokens"] is not None
                if pending and not complete:
                    # The paired response already has a usage report. An
                    # incomplete mirror cannot fill inputs from another baseline.
                    return session, None
                key = pending or f"legacy:{stamp}:{position}"
                return session, Event(0, session, key, stamp, amount=amount,
                                      codex_total=totals[0], codex_legacy=not complete, codex_compatibility=bool(pending),
                                      **input_usage)
        turn = _id(payload.get("turn_id"))
        if kind in ("task_started", "task_complete", "turn_aborted"):
            rollout.pending_response = ""
            if turn:
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
        usage = _dict(message.get("usage"))
        return session, Event(1, session, "assistant:" + identity, stamp, amount=_amount(usage.get("output_tokens")),
                              **_split_input(usage.get("input_tokens"), usage.get("cache_read_input_tokens"),
                                             usage.get("cache_creation_input_tokens")))
    if kind == "user" and not row.get("isMeta") and not row.get("toolUseResult"):
        content = message.get("content")
        if isinstance(content, str) or (isinstance(content, list) and
                                       any(_dict(part).get("type") == "text" for part in content)):
            return session, Event(1, session, "user:" + (_id(row.get("promptId")) or identity), stamp, turn=True)
    if kind == "custom-title":
        return session, Event(1, session, "title:" + identity, stamp, title=_title(row.get("customTitle")))
    return session, None


def _antigravity(row, session, position, stamp, *, metadata=None):
    index = row.get("step_index")
    if stamp is None or type(index) is not int or index < 0:
        return session, None
    kind, origin = row.get("type"), row.get("source")
    if kind == "USER_INPUT" and origin == "USER_EXPLICIT":
        return session, Event(-1, session, f"prompt:{index}", stamp, turn=True)
    if kind == "PLANNER_RESPONSE" and origin == "MODEL":
        model = row.get("model")
        if metadata is not None:
            model = metadata.model_at(index) or model
            stamp = _time(metadata.completed_ms.get(index)) or stamp
        source = ANTIGRAVITY_MODELS.get(model, -1) if isinstance(model, str) else -1
        # Desktop transcript steps report fresh input separately from cached
        # reads. This differs from the Antigravity CLI result envelope.
        return session, Event(source, session, f"planner:{index}", stamp, amount=_amount(row.get("output_tokens")),
                              **_split_input(row.get("input_tokens"), row.get("cache_read_tokens"), 0))
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
        usage = _dict(_dict(row.get("message")).get("usage"))
        amount = _amount(usage.get("output_tokens"))
        if amount is not None:
            key = _id(data.get("messageId")) or identity
            total = _amount(usage.get("total_tokens"))
            input_tokens = _amount(usage.get("input_tokens"))
            cached = usage.get("cache_read_input_tokens")
            # The recorded total identifies whether input already includes
            # cache reads. A missing cache count stays unknown.
            if total is not None and input_tokens is not None:
                if total != input_tokens + amount:
                    read = _amount(cached)
                    input_tokens = total - amount if read is not None and total == input_tokens + read + amount else None
            else:
                input_tokens = None
            return session, Event(5, session, "assistant:" + key, stamp, amount=amount,
                                  **_included_input(input_tokens, cached))
    return session, None


def _jsonl_rows(stream):
    while line := stream.readline():
        if not line.endswith(b"\n"):
            break
        position = stream.tell()
        try:
            row = _dict(json.loads(line))
        except (UnicodeError, ValueError, RecursionError):
            row = {}
        yield position, row


def codex_archive_sibling(path):
    """Find the canonical sibling path for a correctly named Codex rollout."""
    match = CODEX_ROLLOUT_NAME.fullmatch(path.name)
    if not match:
        return None
    year, month, day, identity = match.groups()
    if path.parent.name == "archived_sessions":
        return path.parent.parent / "sessions" / year / month / day / path.name, identity
    if path.parent.parts[-4:] == ("sessions", year, month, day):
        return path.parents[4] / "archived_sessions" / path.name, identity
    return None


def codex_rollout_id(stream):
    _, header = next(_jsonl_rows(stream), (0, {}))
    return _id(_dict(header.get("payload")).get("id")) if header.get("type") == "session_meta" else ""


def _codex_replay_prefix(stream):
    rows = _jsonl_rows(stream)
    position, first = next(((end, row) for end, row in rows if row), (0, {}))
    meta = _dict(first.get("payload"))
    child = _id(meta.get("id"))
    if first.get("type") != "session_meta" or not child or not _id(meta.get("forked_from_id")):
        return 0, False
    foreign_meta = False
    token_reports = 0
    previous_stamp = _time(first.get("timestamp"))
    for end, row in rows:
        stamp = _time(row.get("timestamp"))
        # ccusage uses a foreign session header and 20 uninterrupted reports
        # to identify a copied prefix even when its timestamps were rewritten.
        if stamp is not None and previous_stamp is not None and stamp - previous_stamp >= 1:
            return (position if foreign_meta and token_reports >= 20 else 0), False
        payload = _dict(row.get("payload"))
        kind = row.get("type")
        if kind == "session_meta" and (identity := _id(payload.get("id"))) and identity != child:
            foreign_meta = True
        if kind == "event_msg" and payload.get("type") == "token_count":
            token_reports += 1
        if stamp is not None:
            previous_stamp = stamp
        position = end
    # The next append can decide whether an unfinished prefix was copied.
    return (position if foreign_meta and token_reports >= 20 else 0), True


def jsonl_events(kind, stream, session, *, codex=None, antigravity_metadata=None):
    """Yield complete-line cursor positions alongside normalized events."""
    replay_until = 0
    if kind == "codex":
        if codex is None:
            codex = CodexRollout()
        if stream.tell() == 0:
            replay_until, codex.fork_prefix_pending = _codex_replay_prefix(stream)
            stream.seek(0)
    elif kind != "antigravity":
        parser = {"claude": _claude, "workbuddy": _workbuddy}[kind]
    for position, row in _jsonl_rows(stream):
        stamp = _time(row.get("created_at") if kind == "antigravity" else row.get("timestamp"))
        if kind == "codex":
            if replay_until and position > replay_until:
                replay_until = 0
                codex.pending_response = ""
            session, event = _codex(row, session, position, stamp, codex)
            if replay_until:
                # Keep cumulative counters as the baseline for the child.
                event = None
        elif kind == "antigravity":
            session, event = _antigravity(row, session, position, stamp, metadata=antigravity_metadata)
        else:
            session, event = parser(row, session, position, stamp)
        yield position, session, event


def log_paths(home):
    override = os.environ.get("CODEX_HOME")
    codex_homes = (Path(override).expanduser(),) if override else (
        home / ".codex", *sorted((home / ".antigravity_cockpit/instances/codex").glob("*")),
    )
    # A still-active conversation can have been created before the reporting window.
    for codex_home in codex_homes:
        for folder in ("sessions", "archived_sessions"):
            for path in (codex_home / folder).rglob("*.jsonl"):
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
                tokens = _dict(message.get("tokens"))
                cache = _dict(tokens.get("cache"))
                amount = _amount(tokens.get("output"))
                reasoning = _amount(tokens.get("reasoning"))
                if amount is not None and reasoning is not None:
                    # OpenCode's stored output is visible output only.
                    amount = min(amount + reasoning, MAX_AMOUNT)
                yield Event(4, session, identity, stamp, amount=amount,
                            **_split_input(tokens.get("input"), cache.get("read"), cache.get("write")))
            elif role == "user":
                yield Event(4, session, identity, stamp, turn=True)
            if title:
                yield Event(4, session, "title", stamp, title=_title(title))
