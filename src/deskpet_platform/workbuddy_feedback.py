"""Read explicit WorkBuddy hook events for completion and questions."""
import hashlib
import json
import os
from pathlib import Path
import time

from .claude_feedback import MATCHERS as QUESTION_MATCHERS, TOOL


EDITIONS = {"workbuddy": ".workbuddy", "workbuddy_ai": ".workbuddy-ai"}
TAIL_LIMIT = 256 * 1024
MATCHERS = {**QUESTION_MATCHERS, "Notification": "^permission_prompt$"}


def settings_path(edition):
    return Path.home() / EDITIONS[edition] / "settings.json"


def _latest_completed_turn(path, projects):
    if not isinstance(path, str) or not path:
        return None
    transcript = Path(path).resolve()
    if transcript.suffix != ".jsonl" or not transcript.is_relative_to(projects):
        return None
    if "subagents" in transcript.relative_to(projects).parts or not transcript.is_file():
        return None

    with transcript.open("rb") as stream:
        offset = max(0, stream.seek(0, os.SEEK_END) - TAIL_LIMIT)
        stream.seek(offset)
        lines = stream.read(TAIL_LIMIT).splitlines(keepends=True)
    if offset and lines:
        offset += len(lines.pop(0))
    has_assistant = False
    position = offset + sum(map(len, lines))
    for line in reversed(lines):
        position -= len(line)
        if not line.endswith(b"\n"):
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        provider = entry.get("providerData")
        if isinstance(provider, dict) and provider.get("isMeta"):
            continue
        if entry.get("role") == "assistant":
            has_assistant = True
        elif entry.get("role") == "user":
            if not has_assistant:
                return None
            timestamp = entry.get("timestamp")
            if type(timestamp) is int and 0 < timestamp <= 2**63 - 1:
                return f"prompt-{timestamp}"
            key = f"{transcript.relative_to(projects)}:{position}"
            return "prompt-" + hashlib.sha256(key.encode()).hexdigest()[:40]
    return None


def _all_questions_answered(questions, answers):
    if not isinstance(answers, dict):
        return False
    for question in questions:
        prompt = question.get("question") if isinstance(question, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            return False
        answer = answers.get(prompt)
        if not isinstance(answer, str) or not answer.strip():
            return False
    return True


def deliver(store, event, *, edition="workbuddy"):
    if not isinstance(event, dict):
        return False
    session = event.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        return False
    cwd = event.get("cwd")
    title = Path(cwd).name if isinstance(cwd, str) else ""
    title = title if title.strip() and title.isprintable() else "WorkBuddy"
    kind = event.get("hook_event_name")
    generation = event.get("generation_id")
    permission = None
    if isinstance(generation, str) and 0 < len(generation) <= 128 and generation.isprintable():
        permission = "permission-" + hashlib.sha256(generation.encode()).hexdigest()[:40]
    if kind == "Notification":
        if event.get("notification_type") != "permission_prompt" or permission is None:
            return False
        return bool(store.add_agent_event("workbuddy", session, permission, "response", "授权 · " + title))
    if kind == "Stop":
        if event.get("stop_hook_active") is True or event.get("is_truncation_recovery") is True:
            return False
        resolved = bool(permission and store.resolve_agent_event("workbuddy", session, permission))
        projects = (settings_path(edition).parent / "projects").resolve()
        for attempt in range(5):
            if attempt:
                time.sleep(.05 * attempt)
            identity = _latest_completed_turn(event.get("transcript_path"), projects)
            if identity:
                return bool(store.add_agent_event("workbuddy", session, identity, "completed", title))
        return resolved
    if event.get("tool_name") != TOOL:
        return False
    use = event.get("tool_use_id") or event.get("call_id")
    if not isinstance(use, str) or not 0 < len(use) <= 128 or not use.isprintable():
        return False
    identity = "question-" + hashlib.sha256(use.encode()).hexdigest()[:40]
    if kind == "PreToolUse":
        details = event.get("tool_input")
        questions = details.get("questions") if isinstance(details, dict) else None
        if not isinstance(questions, list) or not questions:
            return False
        # The SDK can deliver PreToolUse with the user's answers already attached.
        if _all_questions_answered(questions, details.get("answers")):
            return bool(store.resolve_agent_event("workbuddy", session, identity))
        return bool(store.add_agent_event("workbuddy", session, identity, "response", title))
    if kind in ("PostToolUse", "PostToolUseFailure"):
        return bool(store.resolve_agent_event("workbuddy", session, identity))
    return False
