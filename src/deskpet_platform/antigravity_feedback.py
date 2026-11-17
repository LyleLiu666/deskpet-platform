"""Receive Antigravity's completion and question hooks."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID

from .codex_hook import _load, _write, generated_hook_target

TRANSCRIPT_LIMIT = 512 * 1024
ANTIGRAVITY_PROFILES = ("antigravity", "antigravity-cli", "antigravity-ide")
QUESTION_TOOL = "ask_question"
HOOK_RESPONSES = {"stop": {"decision": "stop"}, "before": {"decision": "allow"}, "after": {}}


def settings_path():
    return Path.home() / ".gemini/config/hooks.json"


def hook_entry(command):
    entry = {"Stop": [{"type": "command", "command": command, "timeout": 2}]}
    for event, phase in (("PreToolUse", "before"), ("PostToolUse", "after")):
        entry[event] = [{"matcher": QUESTION_TOOL, "hooks": [
            {"type": "command", "command": f"{command} --phase {phase}", "timeout": 2}]}]
    return entry


def _entry_name(command):
    return "deskpet-" + hashlib.sha256(command.encode()).hexdigest()[:16]


def _entry_variants(entry):
    stop_only = {"Stop": entry["Stop"]}
    return (entry, stop_only, {**entry, "enabled": False}, {**stop_only, "enabled": False})


def _entry_command(name, entry):
    try:
        command = entry["Stop"][0]["command"]
    except (IndexError, KeyError, TypeError):
        return None
    if not isinstance(command, str):
        return None
    if name == _entry_name(command) and entry in _entry_variants(hook_entry(command)):
        return command
    return None


def _previous_entries(settings, command, previous_commands=()):
    previous_commands = tuple(previous_commands)
    targets = {generated_hook_target(item) for item in (command, *previous_commands)} - {None}
    name = _entry_name(command)
    for old_name, old_entry in settings.items():
        if old_name == name or not old_name.startswith("deskpet-"):
            continue
        old_command = _entry_command(old_name, old_entry)
        if old_command in previous_commands or generated_hook_target(old_command) in targets:
            yield old_name


def configure(path, command, *, enabled, previous_commands=()):
    if not enabled and not path.exists() and not path.is_symlink():
        return False
    settings, original = _load(path, private=False)
    name = _entry_name(command)
    entry = hook_entry(command)
    previous = settings.get(name)
    if name in settings and previous not in _entry_variants(entry):
        raise ValueError("已有同名 Antigravity hook。请在 Antigravity 设置中检查该条目。")

    obsolete = list(_previous_entries(settings, command, previous_commands))
    for old_name in obsolete:
        del settings[old_name]

    changed = bool(obsolete)
    if enabled and previous != entry:
        settings[name] = entry
        changed = True
    elif not enabled and previous is not None:
        del settings[name]
        changed = True
    if changed:
        _write(path, settings, original=original, private=False)
    return changed


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _transcript_target(session: str, transcript_path: object) -> tuple[str, Path] | None:
    if not isinstance(transcript_path, str) or not transcript_path or not _is_uuid(session):
        return None
    try:
        path = Path(transcript_path).expanduser()
        if path.name not in ("transcript.jsonl", "transcript_full.jsonl"):
            return None
        resolved = path.resolve()
        profiles = Path.home() / ".gemini"
        for profile in ANTIGRAVITY_PROFILES:
            expected = (profiles / profile).resolve() / "brain" / session / ".system_generated" / "logs" / path.name
            if resolved == expected:
                return profile, resolved
    except (OSError, ValueError):
        return None
    return None


def _query_summary_row(db_path: Path, session: str) -> tuple[object, ...] | None:
    if db_path.is_symlink() or not db_path.is_file():
        return None
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
            return db.execute(
                "SELECT title FROM conversation_summaries WHERE conversation_id=?",
                (session,),
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError):
        return None


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())
    if not clean or not clean.isprintable():
        return None
    return clean[:64]


def _read_summary_title(db_path: Path, session: str, *, wait_for_title: bool = False) -> str | None:
    row = _query_summary_row(db_path, session)
    if row is None:
        return None
    if title := _clean_title(row[0]):
        return title
    if not wait_for_title:
        return None
    for delay in (0.05, 0.1, 0.15):
        time.sleep(delay)
        row = _query_summary_row(db_path, session)
        if row is None:
            return None
        if title := _clean_title(row[0]):
            return title
    return None


def _summary_title(session: str, transcript_path: object, *, wait_for_title: bool = False) -> str | None:
    if not _is_uuid(session):
        return None
    target = _transcript_target(session, transcript_path)
    profiles = (target[0],) if target is not None else ANTIGRAVITY_PROFILES
    root = Path.home() / ".gemini"
    for profile in profiles:
        title = _read_summary_title(root / profile / "conversation_summaries.db", session, wait_for_title=wait_for_title)
        if title is not None:
            return title
    return None


def _workspace_title(event: dict) -> str | None:
    workspaces = event.get("workspacePaths")
    workspace = workspaces[0] if isinstance(workspaces, list) and workspaces else None
    if not isinstance(workspace, str):
        return None
    return _clean_title(Path(workspace).name)


def session_title(session: str, event: dict, *, wait_for_title: bool = False) -> str:
    return (
        _summary_title(session, event.get("transcriptPath"), wait_for_title=wait_for_title)
        or _workspace_title(event)
        or "Antigravity"
    )


def _completed_prompt(session, transcript_path):
    target = _transcript_target(session, transcript_path)
    if target is None:
        return None
    _, path = target
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            offset = max(0, stream.seek(0, os.SEEK_END) - TRANSCRIPT_LIMIT)
            stream.seek(offset)
            lines = stream.read(TRANSCRIPT_LIMIT).splitlines()[bool(offset):]
    except (OSError, ValueError):
        return None
    answered = False
    for line in reversed(lines):
        try:
            step = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(step, dict):
            continue
        if step.get("source") == "MODEL" and step.get("type") == "PLANNER_RESPONSE":
            answered |= step.get("status") == "DONE"
        if step.get("source") == "USER_EXPLICIT" and step.get("type") == "USER_INPUT":
            index = step.get("step_index")
            if answered and type(index) is int and index >= 0:
                return f"prompt-{index}"
            return None
    return None


def _deliver_question(store, event, session):
    tool = event.get("toolCall")
    if not isinstance(tool, dict) or tool.get("name") != QUESTION_TOOL:
        return False
    request = tool.get("id")
    if not isinstance(request, str) or not 0 < len(request) <= 128 or not request.isprintable():
        return False
    identity = "question-" + hashlib.sha256(request.encode()).hexdigest()[:40]
    kind = event["hookEventName"]
    if kind == "PostToolUse":
        return bool(store.resolve_agent_event("antigravity", session, identity))
    args = tool.get("args")
    questions = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(questions, list) or not questions:
        return False
    title = session_title(session, event)
    return bool(store.add_agent_event("antigravity", session, identity, "response", title))


def deliver(store, event):
    if not isinstance(event, dict):
        return False
    session = event.get("conversationId")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        return False
    kind = event.get("hookEventName", "Stop")
    if kind in ("PreToolUse", "PostToolUse"):
        return _deliver_question(store, event, session)
    execution = event.get("executionNum")
    if (kind != "Stop" or event.get("fullyIdle") is not True or event.get("error") or
            event.get("continuationActive") or type(execution) is not int or not 0 <= execution <= 2**63 - 1):
        return False
    reason = event.get("terminationReason")
    if reason == "model_stop":
        identity = f"execution-{execution}"
    elif reason == "NO_TOOL_CALL":
        # CLI 1.5.7 restarts executionNum at zero for each user turn.
        identity = _completed_prompt(session, event.get("transcriptPath"))
    else:
        return False
    if identity is None:
        return False
    title = session_title(session, event, wait_for_title=True)
    return bool(store.add_agent_event("antigravity", session, identity, "completed", title))
