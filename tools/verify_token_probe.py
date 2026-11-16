"""Verify a release probe with an isolated synthetic Codex transcript."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify(executable, fixture):
    with tempfile.TemporaryDirectory(prefix="DeskPet probe ") as temporary:
        home = Path(temporary)
        sessions = home / ".codex" / "sessions"
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        folder = sessions / now.strftime("%Y/%m/%d")
        folder.mkdir(parents=True)
        transcript = folder / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-00000000-0000-4000-8000-000000000001.jsonl"
        # Preserve timing between events and keep the synthetic turn recent.
        entries = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            entries.append(item)
        last = datetime.fromisoformat(entries[-1]["timestamp"])
        offset = now - last
        for item in entries:
            item["timestamp"] = (datetime.fromisoformat(item["timestamp"]) + offset).isoformat()
            if item["type"] == "session_meta":
                item["payload"]["timestamp"] = item["timestamp"]
        transcript.write_text("".join(json.dumps(item) + "\n" for item in entries), encoding="utf-8")

        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), APPDATA=str(home),
                   LOCALAPPDATA=str(home), XDG_CONFIG_HOME=str(home), XDG_DATA_HOME=str(home),
                   CODEX_HOME=str(sessions.parent), DOTNET_BUNDLE_EXTRACT_BASE_DIR=str(home / "runtime"))
        captured = subprocess.run([str(executable.resolve()), "--watch", "6", "--codex", str(sessions)],
                                  cwd=home, env=env, capture_output=True, text=True, encoding="utf-8",
                                  timeout=45, check=True)
        snapshots = [json.loads(line) for line in captured.stdout.splitlines()]
        measured = [item for item in snapshots if item.get("state") == "running"
                    and item["t"] > 0 and type(item["estimatedShare"]) in (float, int)
                    and type(item["generating"]) is int and type(item["tools"]) is int]
        if len(measured) < 2 or not any(item["live"]["output"] > 0 for item in measured):
            raise RuntimeError(f"Probe did not stream measured snapshots: {snapshots}")
        if not any(item["today"]["output"] >= 530 for item in measured):
            raise RuntimeError("The probe did not report the expected synthetic token total.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("fixture", type=Path)
    arguments = parser.parse_args()
    verify(arguments.executable, arguments.fixture)
