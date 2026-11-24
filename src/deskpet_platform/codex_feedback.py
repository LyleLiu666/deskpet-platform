"""Record explicit Codex question-tool events without storing conversation text."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .notifications import codex_home, session_title


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


def _closed_async_call(output):
    if isinstance(output, str):
        # This is Codex's tool-argument decoder diagnostic, scoped to an exact call ID.
        if output.startswith("failed to parse function arguments: "):
            return True
        try:
            output = json.loads(output)
        except ValueError:
            return False
    return isinstance(output, dict) and output.get("accepted") is False


class QuestionReconciler:
    """Recover missed hook outcomes from the local Codex session records."""

    def __init__(self, store, home=None):
        self.store = store
        self.home = Path(home) if home is not None else codex_home()
        self.checked = {}

    def run(self):
        pending = {}
        for session, identity, turn in self.store.open_codex_questions():
            pending.setdefault(session, {})[identity] = turn
        if not pending:
            self.checked.clear()
            return 0
        try:
            state = (self.home / "state_5.sqlite").resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(state, uri=True, timeout=.1)) as db:
                threads = {session: db.execute("SELECT rollout_path,archived FROM threads WHERE id=?",
                                               (session,)).fetchone() for session in pending}
        except (OSError, sqlite3.Error, ValueError):
            return 0
        updated, checked = 0, {}
        for session, questions in pending.items():
            if threads[session] is None:
                continue
            filename, archived = threads[session]
            if archived == 1:
                settled, abandoned = set(), questions
            else:
                settled, abandoned, progress = self._read(session, filename, questions)
                if progress is not None:
                    checked[session] = progress
            if abandoned:
                updated += self.store.resolve_agent_events("codex", session, abandoned,
                                                           remember_missing=False, abandoned=True)
            if settled:
                updated += self.store.resolve_agent_events("codex", session, settled.difference(abandoned),
                                                           remember_missing=False)
        self.checked = checked
        return updated

    def _read(self, session, filename, questions):
        if not isinstance(filename, str) or not filename:
            return set(), set(), None
        try:
            path = Path(filename).resolve()
            if path.suffix != ".jsonl" or not path.is_relative_to((self.home / "sessions").resolve()):
                return set(), set(), None
            stat = path.stat()
            if not path.is_file():
                return set(), set(), None
            key = (path, stat.st_dev, stat.st_ino)
            targets = frozenset(questions.items())
            previous, offset, known = self.checked.get(session, (None, 0, frozenset()))
            if key != previous or offset > stat.st_size or not targets.issubset(known):
                offset = 0
            settled, abandoned = set(), set()
            with path.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    offset += len(line)
                    answered, interrupted = self._outcomes(line, questions)
                    settled.update(answered)
                    abandoned.update(interrupted)
        except (OSError, ValueError, RuntimeError):
            return set(), set(), None
        return settled, abandoned, (key, offset, targets)

    @staticmethod
    def _outcomes(line, questions):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return (), ()
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            return (), ()
        kind = payload.get("type")
        if record.get("type") == "event_msg":
            if kind == "turn_aborted" and isinstance(payload.get("turn_id"), str):
                return (), [identity for identity, turn in questions.items() if turn == payload["turn_id"]]
            if kind == "user_message":
                return [identity for identity in answers(payload.get("message")) if identity in questions], ()
        if record.get("type") == "response_item" and kind == "function_call_output" and _closed_async_call(payload.get("output")):
            try:
                return [identity for index in range(3)
                        if (identity := question_id(payload.get("call_id"), index)) in questions], ()
            except ValueError:
                return (), ()
        return (), ()


def deliver(store, event):
    if not isinstance(event, dict):
        raise ValueError("invalid_feedback_event")
    session = event.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 128 or not session.isprintable():
        raise ValueError("invalid_feedback_session")
    kind = event.get("hook_event_name")
    if kind == "UserPromptSubmit":
        return store.resolve_agent_events("codex", session, answers(event.get("prompt")))
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
        if _closed_async_call(response):
            return store.resolve_agent_events("codex", session, identities)
        return 0
    turn = event.get("turn_id")
    if not isinstance(turn, str) or not 0 < len(turn) <= 128 or not turn.isprintable():
        raise ValueError("invalid_feedback_turn")
    title = session_title(session, event.get("cwd"))
    cwd = event.get("cwd")
    project = Path(cwd).name if isinstance(cwd, str) else ""
    if project.strip() and project.isprintable() and project != title:
        title = f"{project} · {title}"
    store.add_agent_events("codex", session, identities, "response", title,
                           parent_turn=turn, response_group=identities[0], workspace=cwd)
    return len(identities)
