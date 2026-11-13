"""Translate Claude Code hook events into the board's notification records."""
import hashlib
import json
import os
from pathlib import Path
import time


TOOL = "AskUserQuestion"
MATCHERS = {"Stop": None, "PreToolUse": TOOL, "PostToolUse": TOOL, "PostToolUseFailure": TOOL,
            "UserPromptSubmit": None}


def settings_path():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"


def _transcript_entries(event):
    path_value = event.get("transcript_path")
    if not isinstance(path_value, str) or not path_value:
        return
    try:
        path = Path(path_value).expanduser().resolve()
        projects = (settings_path().parent / "projects").resolve()
        if path.suffix != ".jsonl" or not path.is_relative_to(projects) or not path.is_file():
            return
        with path.open("rb") as stream:
            limit = 256 * 1024
            offset = max(0, stream.seek(0, os.SEEK_END) - limit)
            stream.seek(offset)
            lines = stream.read(limit).splitlines()[bool(offset):]
    except (OSError, ValueError, RuntimeError):
        return
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(entry, dict) and entry.get("sessionId") == event["session_id"] and not entry.get("isSidechain"):
            yield entry


def _turn_id(event):
    prompt_id = event.get("prompt_id")
    if isinstance(prompt_id, str) and 0 < len(prompt_id) <= 120 and prompt_id.isprintable():
        return "prompt-" + prompt_id
    # Older Claude Code versions omit a turn ID. The assistant UUID is stable
    # across duplicate deliveries of the same Stop.
    for entry in _transcript_entries(event):
        if entry.get("type") == "user":
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) or (isinstance(content, list) and
                    any(isinstance(block, dict) and block.get("type") == "text" for block in content)):
                return None
        if entry.get("type") == "assistant":
            identity = entry.get("uuid")
            if isinstance(identity, str) and 0 < len(identity) <= 118 and identity.isprintable():
                return "assistant-" + identity
    return None


def _question_id(use):
    if isinstance(use, str) and 0 < len(use) <= 128 and use.isprintable():
        return "question-" + hashlib.sha256(use.encode()).hexdigest()[:40]
    return None


def _settle_dismissed_questions(store, event):
    # Claude Code 2.1.281 records Escape as a rejected tool result without
    # firing PostToolUse or PostToolUseFailure. Inspect only native result IDs
    # when the next hook arrives. Prompt and result text stay out of the inbox.
    dismissed = set()
    for entry in _transcript_entries(event):
        if entry.get("type") != "user" or entry.get("toolDenialKind") != "user-rejected":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") is True:
                identity = _question_id(block.get("tool_use_id"))
                if identity:
                    dismissed.add(identity)
    return bool(dismissed and store.resolve_agent_events(
        "claude_code", event["session_id"], dismissed, remember_missing=False))


def deliver(store, event):
    if not isinstance(event, dict):
        return False
    session = event.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        return False
    cwd = event.get("cwd")
    title = Path(cwd).name if isinstance(cwd, str) else ""
    title = title if title.strip() and title.isprintable() else "Claude Code"
    kind = event.get("hook_event_name")
    if kind == "UserPromptSubmit":
        return _settle_dismissed_questions(store, event)
    if kind == "Stop":
        if event.get("stop_hook_active") is True:
            return False
        settled = _settle_dismissed_questions(store, event)
        identity = None
        for attempt in range(5):
            if attempt:
                time.sleep(.05 * attempt)
            identity = _turn_id(event)
            if identity:
                break
        completed = identity and store.add_agent_event("claude_code", session, identity, "completed", title)
        return bool(completed or settled)
    if event.get("tool_name") != TOOL:
        return False
    identity = _question_id(event.get("tool_use_id"))
    if identity is None:
        return False
    if kind == "PreToolUse":
        details = event.get("tool_input")
        if isinstance(details, dict) and isinstance(details.get("questions"), list) and details["questions"]:
            return bool(store.add_agent_event("claude_code", session, identity, "response", title))
    if kind in ("PostToolUse", "PostToolUseFailure"):
        return bool(store.resolve_agent_event("claude_code", session, identity))
    return False
