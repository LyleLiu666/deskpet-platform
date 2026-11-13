"""Translate Claude Code hook events into the board's notification records."""
import hashlib
import json
import os
from pathlib import Path
import time


TOOL = "AskUserQuestion"
MATCHERS = {"Stop": None, "PreToolUse": TOOL, "PostToolUse": TOOL, "PostToolUseFailure": TOOL}


def settings_path():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"


def _turn_id(event):
    prompt_id = event.get("prompt_id")
    if isinstance(prompt_id, str) and 0 < len(prompt_id) <= 120 and prompt_id.isprintable():
        return "prompt-" + prompt_id
    path_value = event.get("transcript_path")
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value).expanduser().resolve()
    projects = (settings_path().parent / "projects").resolve()
    if path.suffix != ".jsonl" or not path.is_relative_to(projects) or not path.is_file():
        return None
    # Earlier Claude Code versions omit a turn ID. Read a bounded transcript
    # tail to locate the last assistant UUID. Conversation text stays local.
    with path.open("rb") as stream:
        offset = max(0, stream.seek(0, os.SEEK_END) - 256 * 1024)
        stream.seek(offset)
        data = stream.read(256 * 1024)
    lines = data.splitlines()[bool(offset):]
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or entry.get("sessionId") != event["session_id"] or entry.get("isSidechain"):
            continue
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
    if kind == "Stop":
        if event.get("stop_hook_active") is True:
            return False
        identity = None
        for attempt in range(5):
            if attempt:
                time.sleep(.05 * attempt)
            identity = _turn_id(event)
            if identity:
                break
        return bool(identity and store.add_agent_event("claude_code", session, identity, "completed", title))
    if event.get("tool_name") != TOOL:
        return False
    use = event.get("tool_use_id")
    if not isinstance(use, str) or not 0 < len(use) <= 128 or not use.isprintable():
        return False
    identity = "question-" + hashlib.sha256(use.encode()).hexdigest()[:40]
    if kind == "PreToolUse":
        details = event.get("tool_input")
        if isinstance(details, dict) and isinstance(details.get("questions"), list) and details["questions"]:
            return bool(store.add_agent_event("claude_code", session, identity, "response", title))
    if kind in ("PostToolUse", "PostToolUseFailure"):
        return bool(store.resolve_agent_event("claude_code", session, identity))
    return False
