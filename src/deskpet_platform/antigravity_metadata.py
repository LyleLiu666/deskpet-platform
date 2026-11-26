"""Read model identities and completion times from a companion SQLite database."""

from bisect import bisect_left
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
import stat

from ._agent_speed_protobuf import Message, ProtobufError


@dataclass(frozen=True, slots=True)
class AntigravityMetadata:
    by_step: dict[int, str | None]
    executor_ends: tuple[int, ...]
    executor_models: tuple[str | None, ...]
    completed_ms: dict[int, int]

    def model_at(self, step):
        model = self.by_step.get(step)
        if model:
            return model
        index = bisect_left(self.executor_ends, step)
        return self.executor_models[index] if index < len(self.executor_models) else None


def companion_database(transcript: Path) -> Path:
    return transcript.parents[4] / "conversations" / (transcript.parents[2].name + ".db")


def metadata_revision(path: Path) -> str | None:
    identities = []
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            info = candidate.lstat()
        except OSError:
            if candidate == path:
                return None
            continue
        if not stat.S_ISREG(info.st_mode):
            return None
        identities.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return json.dumps(identities)


def _completion_time(metadata):
    times = {field: metadata.timestamp_ms(field) for field in (1, 6, 7, 8, 22, 32)}
    # Match the AgentSpeed end time for a model response.
    return times[7] or times[22] or times[8] or max((time for time in times.values() if time is not None),
                                                    default=None)


def read_metadata(path: Path) -> AntigravityMetadata:
    """Use AgentSpeed's model lookup and completion time for each step."""
    wal = path.with_name(path.name + "-wal")
    with path.open("rb") as header:
        uses_wal = header.read(20)[18:20] == b"\x02\x02"
    # Do not let SQLite create -wal or -shm for a closed conversation.
    immutable = "&immutable=1" if uses_wal and not wal.exists() else ""
    by_generation = {}
    executors = {}
    completed_ms = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro" + immutable, uri=True, timeout=0.2)) as db, db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables.intersection(("gen_metadata", "executor_metadata", "steps")):
            raise sqlite3.DatabaseError("unavailable Antigravity conversation metadata")
        if "gen_metadata" in tables:
            for index, blob in db.execute("SELECT COALESCE(CAST(idx AS INTEGER),0),data FROM gen_metadata "
                                          "WHERE typeof(size)='integer' AND size<=1048576 "
                                          "AND typeof(data)='blob' AND length(data)<=1048576 ORDER BY idx,rowid"):
                try:
                    message = Message(blob)
                    chat = message.message(1)
                    model = (chat.string(19) or chat.string(21) or None) if chat else None
                    by_generation[index] = (message.repeated_uint(2), model)
                except ProtobufError:
                    continue
        if "steps" in tables:
            for index, blob in db.execute("SELECT COALESCE(CAST(idx AS INTEGER),0),metadata FROM steps "
                                          "WHERE step_type=15 AND status NOT IN (1,2,8,9,11) "
                                          "AND typeof(metadata)='blob' AND length(metadata)<=1048576 "
                                          "ORDER BY idx,rowid"):
                try:
                    if (finished := _completion_time(Message(blob))) is not None:
                        completed_ms[index] = finished
                except ProtobufError:
                    continue
        if "executor_metadata" in tables:
            for index, blob in db.execute("SELECT COALESCE(CAST(idx AS INTEGER),0),data FROM executor_metadata "
                                          "WHERE typeof(data)='blob' "
                                          "AND length(data)<=1048576 ORDER BY idx,rowid"):
                try:
                    message = Message(blob)
                    configuration = message.message(10)
                    planner = configuration.message(1) if configuration else None
                    executors[index] = message.integer(3), (planner.string(28) or None) if planner else None
                except ProtobufError:
                    continue
    by_step = {}
    for indices, model in by_generation.values():
        for index in indices:
            by_step.setdefault(index, model)
    ordered = sorted((end, index, model) for index, (end, model) in executors.items())
    return AntigravityMetadata(by_step, tuple(item[0] for item in ordered), tuple(item[2] for item in ordered), completed_ms)
