"""Record explicit Codex question-tool events without storing conversation text."""
import hashlib
import json

from .notifications import session_title


TOOL = "request_user_input_async"
CLI_TOOL = "request_user_input"
OPEN = "<send_user_message_question_reply>"
CLOSE = "</send_user_message_question_reply>"


def question_id(call, index):
    if (not isinstance(call, str) or not 0 < len(call) <= 128 or not call.isprintable() or
            type(index) is not int or not 0 <= index < 3):
        raise ValueError("invalid_question_identity")
    return "question-" + hashlib.sha256((call + ":" + str(index)).encode()).hexdigest()[:40]


def answer_references(prompt):
    if not isinstance(prompt, str):
        return []
    prompt = prompt.strip()
    if not prompt.startswith(OPEN) or not prompt.endswith(CLOSE):
        return []
    try:
        values = json.loads(prompt[len(OPEN):-len(CLOSE)])
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    found = []
    for value in values:
        if not isinstance(value, dict) or "answer" not in value:
            continue
        try:
            identity = json.loads(value["questionItemId"])
            if not isinstance(identity, list) or len(identity) != 3 or identity[0] != TOOL:
                continue
            question_id(identity[1], identity[2])
            found.append((identity[1], identity[2]))
        except (KeyError, TypeError, ValueError):
            continue
    return list(dict.fromkeys(found))


def answers(prompt):
    return [question_id(call, index) for call, index in answer_references(prompt)]


def cli_question_outcomes(questions, response):
    """Return exact question-index resolutions from Codex's synchronous tool."""
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3 or not all(isinstance(q, dict) for q in questions):
        return ()
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return ()
    if not isinstance(response, dict):
        return ()
    if response.get("accepted") is False:
        return ("cancelled",) * len(questions)
    answers = response.get("answers")
    if not isinstance(answers, dict):
        return ()
    identifiers = [question.get("id") for question in questions]
    if not all(isinstance(identity, str) and identity for identity in identifiers):
        return ()
    if len(set(identifiers)) != len(identifiers) or answers.keys() - set(identifiers):
        return ()
    outcomes = []
    for identity in identifiers:
        if identity not in answers:
            outcomes.append("cancelled")
            continue
        item = answers[identity]
        values = item.get("answers") if isinstance(item, dict) else None
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return ()
        outcomes.append("answered" if any(value.strip() for value in values) else "cancelled")
    return tuple(outcomes)


def deliver(store, event):
    if not isinstance(event, dict):
        raise ValueError("invalid_feedback_event")
    session = event.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        raise ValueError("invalid_feedback_session")
    kind = event.get("hook_event_name")
    if kind == "UserPromptSubmit":
        return sum(store.resolve_agent_event("codex", session, identity) for identity in answers(event.get("prompt")))
    if kind not in ("PreToolUse", "PostToolUse") or event.get("tool_name") not in (TOOL, CLI_TOOL):
        return 0
    details = event.get("tool_input")
    questions = details.get("questions") if isinstance(details, dict) else None
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3 or not all(isinstance(q, dict) for q in questions):
        raise ValueError("invalid_feedback_questions")
    identities = [question_id(event.get("tool_use_id"), index) for index in range(len(questions))]
    if kind == "PostToolUse":
        response = event.get("tool_response")
        if event["tool_name"] == CLI_TOOL:
            if cli_question_outcomes(questions, response):
                # The device inbox only records whether an item remains pending.
                return store.resolve_agent_events("codex", session, identities)
            return 0
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                return 0
        if isinstance(response, dict) and response.get("accepted") is False:
            return sum(store.resolve_agent_event("codex", session, identity) for identity in identities)
        return 0
    turn = event.get("turn_id")
    if not isinstance(turn, str) or not 0 < len(turn) <= 128 or not turn.isprintable():
        raise ValueError("invalid_feedback_turn")
    title = session_title(session, event.get("cwd"))
    store.add_agent_events("codex", session, identities, "response", title,
                           parent_turn=turn, response_group=identities[0])
    return len(identities)
