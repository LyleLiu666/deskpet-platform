"""Verify that the installed desktop speed reader counts each new source."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from deskpet_platform.agent_speed_probe import AgentSpeedProbe


def devin(home):
    data = home if os.name == "nt" else home / "data"
    database = data / "devin/cli/sessions.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT, model TEXT);
            CREATE TABLE message_nodes (
                row_id INTEGER PRIMARY KEY, session_id TEXT, chat_message TEXT, created_at INTEGER);
        """)
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("devin-session", str(home), "swe-1-6"))

    def finish():
        reply = json.dumps({
            "role": "assistant",
            "metadata": {"request_id": "devin-call", "finish_reason": "stop",
                         "metrics": {"input_tokens": 200, "output_tokens": 340, "total_time_ms": 2000}},
        })
        row = ("devin-session", reply, int(time.time()))
        with closing(sqlite3.connect(database)) as db, db:
            db.executemany("INSERT INTO message_nodes(session_id, chat_message, created_at) VALUES (?, ?, ?)",
                           [row, row])

    return finish


def qoder_ide(home):
    data = home if os.name == "nt" else home / "config"
    if sys.platform == "darwin":
        data = home / "Library/Application Support"
    database = data / "Qoder/SharedClientCache/cache/db/local.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TABLE chat_session (session_id TEXT PRIMARY KEY, project_uri TEXT);
            CREATE TABLE chat_message (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT, token_info TEXT, model_info TEXT,
                gmt_create INTEGER);
        """)
        db.execute("INSERT INTO chat_session VALUES (?, ?)", ("ide-session", home.as_uri()))

    def finish():
        now = time.time_ns() // 1_000_000
        tokens = json.dumps({"prompt_tokens": 200, "completion_tokens": 410})
        with closing(sqlite3.connect(database)) as db, db:
            db.executemany("INSERT INTO chat_message VALUES (?, ?, ?, ?, ?, ?)", [
                ("prompt", "ide-session", "user", None, None, now - 2000),
                ("reply", "ide-session", "assistant", tokens, '{"model_key":"auto"}', now),
            ])

    return finish


def qoder_cli(home):
    session = "00000000-0000-4000-8000-000000000001"
    transcript = home / "qoder-cli/projects/synthetic" / f"{session}.jsonl"
    transcript.parent.mkdir(parents=True)
    prompt_at = datetime.now(timezone.utc)
    prompt = {"type": "user", "sessionId": session, "cwd": str(home), "timestamp": prompt_at.isoformat(),
              "uuid": "00000000-0000-4000-8000-000000000002",
              "message": {"role": "user", "content": "Synthetic prompt"}}
    transcript.write_text(json.dumps(prompt) + "\n", encoding="utf-8")

    def finish():
        reply = {
            "type": "assistant", "sessionId": session, "cwd": str(home),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uuid": "00000000-0000-4000-8000-000000000003", "parentUuid": prompt["uuid"],
            "message": {"id": "qoder-cli-call", "role": "assistant", "model": "auto",
                        "content": [{"type": "text", "text": "Synthetic reply"}], "stop_reason": "end_turn",
                        "usage": {"input_tokens": 200, "output_tokens": 290}},
        }
        with transcript.open("a", encoding="utf-8") as file:
            file.write(json.dumps(reply) + "\n")

    return finish


async def wait_for_speed(probe, *, measured=False):
    async with asyncio.timeout(20):
        while True:
            sample = await probe.sample()
            if sample is not None and (
                not measured or (sample["rate10"] > 0 and probe.speed_state == "L")
            ):
                return sample
            await asyncio.sleep(0.2)


async def verify(executable):
    for source, fixture in (("Devin CLI", devin), ("Qoder IDE", qoder_ide), ("Qoder CLI", qoder_cli)):
        with tempfile.TemporaryDirectory(prefix="deskpet-speed-") as directory:
            home = Path(directory).resolve()
            finish = fixture(home)
            environment = dict(HOME=str(home), USERPROFILE=str(home), CFFIXED_USER_HOME=str(home),
                               APPDATA=str(home), LOCALAPPDATA=str(home), XDG_DATA_HOME=str(home / "data"),
                               XDG_CONFIG_HOME=str(home / "config"), CODEX_HOME=str(home / "codex"),
                               CHISEL_SESSION_DB="", QODER_CONFIG_DIR=str(home / "qoder-cli"),
                               QODERCN_CONFIG_DIR=str(home / "qoder-cn"),
                               DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(home / "runtime"))
            with patch.dict(os.environ, environment):
                probe = AgentSpeedProbe(executable)
                try:
                    await wait_for_speed(probe)
                    finish()
                    sample = await wait_for_speed(probe, measured=True)
                except TimeoutError as error:
                    raise RuntimeError(f"{source} did not contribute to total speed") from error
                finally:
                    await probe._close()
            print(f"{source} contributes to the desktop total speed ({sample['rate10'] / 10:g} t/s)", flush=True)


if __name__ == "__main__":
    asyncio.run(verify(Path(sys.argv[1])))
