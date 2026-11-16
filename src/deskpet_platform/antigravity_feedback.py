"""Receive Antigravity's completion and question hooks."""
import hashlib
import json
import os
from pathlib import Path
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
    expected_name = "deskpet-" + hashlib.sha256(command.encode()).hexdigest()[:16]
    if name == expected_name and entry in _entry_variants(hook_entry(command)):
        return command
    return None


def configure(path, command, *, enabled, previous_commands=()):
    if not enabled and not path.exists() and not path.is_symlink():
        return False
    settings, original = _load(path, private=False)
    name = "deskpet-" + hashlib.sha256(command.encode()).hexdigest()[:16]
    entry = hook_entry(command)
    previous = settings.get(name)
    if name in settings and previous not in _entry_variants(entry):
        raise ValueError("已有同名 Antigravity hook。请在 Antigravity 设置中检查该条目。")

    targets = {generated_hook_target(item) for item in (command, *previous_commands)} - {None}
    obsolete = []
    for old_name, old_entry in settings.items():
        if old_name == name or not old_name.startswith("deskpet-"):
            continue
        old_command = _entry_command(old_name, old_entry)
        if old_command in previous_commands or generated_hook_target(old_command) in targets:
            obsolete.append(old_name)
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


def _completed_prompt(session, transcript_path):
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        if str(UUID(session)) != session:
            return None
        path = Path(transcript_path).expanduser()
        if path.name not in ("transcript.jsonl", "transcript_full.jsonl"):
            return None
        path = path.resolve()
        profiles = Path.home() / ".gemini"
        if not any(path == (profiles / profile).resolve() / "brain" / session /
                   ".system_generated" / "logs" / path.name for profile in ANTIGRAVITY_PROFILES):
            return None
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


def _deliver_question(store, event, session, title):
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
    return bool(store.add_agent_event("antigravity", session, identity, "response", title))


def deliver(store, event):
    if not isinstance(event, dict):
        return False
    session = event.get("conversationId")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        return False
    workspaces = event.get("workspacePaths")
    workspace = workspaces[0] if isinstance(workspaces, list) and workspaces else None
    title = Path(workspace).name if isinstance(workspace, str) else ""
    title = title if title.strip() and title.isprintable() else "Antigravity"
    kind = event.get("hookEventName", "Stop")
    if kind in ("PreToolUse", "PostToolUse"):
        return _deliver_question(store, event, session, title)
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
    return bool(store.add_agent_event("antigravity", session, identity, "completed", title))
