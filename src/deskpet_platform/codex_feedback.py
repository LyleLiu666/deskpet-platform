"""Record explicit Codex question-tool events without storing conversation text."""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

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


def _resolve_reply(store, session, identities):
    if not identities:
        return 0
    try:
        # Leave enough time in the two-second hook to save a retry receipt.
        return store.resolve_agent_events("codex", session, identities, timeout=.1)
    except sqlite3.OperationalError as error:
        directory = store.path.parent / "codex-replies"
        if os.name == "nt":
            from .windows_private import ensure_private_directory
            store.path.parent.parent.mkdir(parents=True, exist_ok=True)
            ensure_private_directory(store.path.parent)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("invalid_codex_reply_directory")
        data = json.dumps([session, identities]).encode()
        stream = tempfile.NamedTemporaryFile(dir=directory, delete=False)
        temporary = Path(stream.name)
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(directory / (hashlib.sha256(data).hexdigest() + ".json"))
        finally:
            temporary.unlink(missing_ok=True)
        logging.getLogger(__name__).warning("Codex reply queued for retry (%s)",
                                            getattr(error, "sqlite_errorname", "storage_unavailable"))
        return len(identities)  # Wake the delivery workers even for the last reply.


def retry_replies(store):
    directory = store.path.parent / "codex-replies"
    if not directory.is_dir():
        return 0
    if directory.is_symlink():
        raise ValueError("invalid_codex_reply_directory")
    updated = 0
    for receipt in directory.glob("*.json"):
        try:
            data = receipt.read_bytes()
        except FileNotFoundError:
            continue  # Another worker already committed the same receipt.
        if receipt.stem != hashlib.sha256(data).hexdigest():
            try:
                receipt.replace(receipt.with_suffix(".invalid"))
            except FileNotFoundError:
                continue
            logging.getLogger(__name__).warning("Damaged Codex reply receipt set aside")
            continue
        session, identities = json.loads(data)
        updated += store.resolve_agent_events("codex", session, identities)
        receipt.unlink(missing_ok=True)
    return updated


class QuestionReconciler:
    """Recover missed hook outcomes from the local Codex session records."""

    def __init__(self, store, home=None):
        self.store = store
        self.home = Path(home) if home is not None else codex_home()
        self.checked = {}

    def run(self):
        updated = retry_replies(self.store)
        pending = {}
        for session, identity, turn in self.store.open_codex_questions():
            pending.setdefault(session, {})[identity] = turn
        if not pending:
            self.checked.clear()
            return updated
        try:
            state = (self.home / "state_5.sqlite").resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(state, uri=True, timeout=.1)) as db:
                threads = {session: db.execute("SELECT rollout_path,archived FROM threads WHERE id=?",
                                               (session,)).fetchone() for session in pending}
        except (OSError, sqlite3.Error, ValueError):
            return updated
        checked = {}
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
            previous, offset, known, cli = self.checked.get(session, (None, 0, frozenset(), {}))
            if key != previous or offset > stat.st_size or not targets.issubset(known):
                offset, cli = 0, {}
            else:
                cli = cli.copy()
            settled, abandoned = set(), set()
            with path.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    offset += len(line)
                    answered, interrupted = self._outcomes(line, questions, cli)
                    settled.update(answered)
                    abandoned.update(interrupted)
        except (OSError, ValueError, RuntimeError):
            return set(), set(), None
        return settled, abandoned, (key, offset, targets, cli)

    @staticmethod
    def _outcomes(line, questions, cli):
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
        if record.get("type") != "response_item":
            return (), ()
        if kind == "function_call" and payload.get("name") == CLI_TOOL:
            call = payload.get("call_id")
            try:
                if not any(question_id(call, index) in questions for index in range(3)):
                    return (), ()
                arguments = json.loads(payload.get("arguments"))
            except (TypeError, ValueError):
                return (), ()
            items = arguments.get("questions") if isinstance(arguments, dict) else None
            if isinstance(items, list) and 1 <= len(items) <= 3 and all(isinstance(item, dict) for item in items):
                cli[call] = [{"id": item.get("id")} for item in items]
        if kind == "message" and payload.get("role") == "user":
            content = payload.get("content")
            if isinstance(content, list):
                return {identity for part in content if isinstance(part, dict) and part.get("type") == "input_text"
                        for identity in answers(part.get("text")) if identity in questions}, ()
        if kind == "function_call_output":
            call = payload.get("call_id")
            output = payload.get("output")
            items = cli.get(call) if isinstance(call, str) else None
            if items and cli_question_outcomes(items, output):
                count = len(cli.pop(call))
            elif _closed_async_call(output):
                count = 3
            else:
                return (), ()
            try:
                return [identity for index in range(count) if (identity := question_id(call, index)) in questions], ()
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
        return _resolve_reply(store, session, answers(event.get("prompt")))
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
                return _resolve_reply(store, session, identities)
            return 0
        if _closed_async_call(response):
            return _resolve_reply(store, session, identities)
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
