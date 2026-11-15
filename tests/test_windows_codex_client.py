"""Opt-in Windows Codex rehearsal using synthetic credentials and a local API."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest


class FakeResponsesAPI(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.paths.append(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "Synthetic reply", "annotations": []}]}
        response = {"id": "resp_fixture", "object": "response", "created_at": 1, "status": "completed",
                    "model": body.get("model", "fixture"), "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
        events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                  {"type": "response.output_item.done", "output_index": 0, "item": message},
                  {"type": "response.completed", "response": response}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()


@unittest.skipUnless(os.name == "nt" and os.environ.get("DESKPET_REHEARSE_CODEX") == "1",
                     "explicit isolated Windows Codex rehearsal only")
class WindowsCodexClientRehearsal(unittest.TestCase):
    def test_real_codex_delivers_stop_to_isolated_inbox(self):
        import sys
        from deskpet_platform.codex_hook import configure_codex_hooks
        from deskpet_platform.notifications import NotificationStore
        from deskpet_platform.windows_hook_command import build

        cli = Path(os.environ["DESKPET_SYNTHETIC_CODEX_CLI"])
        self.assertTrue(cli.is_file(), "isolated native Codex executable is missing")
        with tempfile.TemporaryDirectory(prefix="DeskPet Codex fixture ") as directory:
            root = Path(directory)
            home, project = root / "codex-home", root / "project with spaces"
            home.mkdir(); project.mkdir()
            config = root / "isolated state" / "device.json"
            hook = build(sys.executable, ["-m", "deskpet_platform", "--config", str(config), "codex-hook"])
            configure_codex_hooks(home / "hooks.json", hook, [], enabled=True)
            with ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesAPI) as server:
                server.paths = []
                service = Thread(target=server.serve_forever, daemon=True)
                service.start()
                (home / "config.toml").write_text(
                    'model="fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\nname="Fixture"\n'
                    f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                    'env_key="OPENAI_API_KEY"\nwire_api="responses"\n', encoding="utf-8")
                env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
                       if key in os.environ}
                env.update({"CODEX_HOME": str(home), "USERPROFILE": str(root), "HOME": str(root),
                            "APPDATA": str(root), "LOCALAPPDATA": str(root), "TEMP": str(root), "TMP": str(root),
                            "OPENAI_API_KEY": "synthetic-local-key", "NO_PROXY": "127.0.0.1,localhost",
                            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                            "ALL_PROXY": "http://127.0.0.1:9", "OTEL_SDK_DISABLED": "true"})
                args = [str(cli), "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust",
                        "--sandbox", "read-only", "-C", str(project), "Local fixture"]
                try:
                    result = subprocess.run(args, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=75)
                    self.assertEqual(result.returncode, 0, result.stderr[-2500:])
                    self.assertIn("Synthetic reply", result.stdout)
                    self.assertTrue(any(path.startswith("/v1/responses") for path in server.paths), server.paths)
                    notices = NotificationStore(config.parent / "notifications.sqlite3")
                    self.assertTrue(any(item["source"] == "codex" and item["category"] == "completed"
                                        for item in notices.list()), "actual Codex Stop hook did not reach isolated inbox")
                finally:
                    server.shutdown()
                    service.join(timeout=2)
