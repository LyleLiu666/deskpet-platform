"""Opt-in Windows rehearsal: real Claude CLI, isolated settings and local fake API."""
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
import unittest


class FakeClaudeAPI(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.paths.append(self.path)
        if self.path.startswith("/v1/messages/count_tokens"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":12}')
            return
        if not self.path.startswith("/v1/messages"):
            self.send_error(404)
            return
        message = {"type": "message", "id": "msg_fixture", "role": "assistant", "model": body.get("model", "fixture"),
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 12, "output_tokens": 1}}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Fixture done."}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                   "usage": {"output_tokens": 7}}, {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()


@unittest.skipUnless(os.name == "nt" and os.environ.get("DESKPET_REHEARSE_CLAUDE") == "1",
                     "explicit isolated Windows Claude CLI rehearsal only")
class WindowsClaudeClientRehearsal(unittest.TestCase):
    def test_real_client_invokes_wrapped_statusline_against_fake_api(self):
        import sys
        import ctypes
        from winpty import PtyProcess
        from deskpet_platform.quota_capture import QuotaStore
        from deskpet_platform.quota_statusline import configure

        cli = Path(os.environ["DESKPET_SYNTHETIC_CLAUDE_CLI"])
        bash = shutil.which("bash.exe")
        self.assertTrue(cli.is_file() and bash,
                        f"fixture prerequisites: cli={cli.is_file()} bash={bool(bash)}; "
                        f"package entries={[p.name for p in cli.parent.iterdir()] if cli.parent.is_dir() else []}")
        with tempfile.TemporaryDirectory(prefix="DeskPet Claude fixture ") as temporary:
            root = Path(temporary)
            project, profile = root / "project with spaces", root / "profile"
            project.mkdir(); profile.mkdir()
            old = root / "old status.py"
            observed = root / "old invoked.json"
            old.write_text("import json,pathlib,sys\np=json.load(sys.stdin.buffer)\n"
                           "pathlib.Path(sys.argv[1]).write_text(json.dumps({'has_model':isinstance(p.get('model'),dict)}))\n"
                           "print('ORIGINAL-STATUS')\n", encoding="utf-8")
            shell_path = lambda value: str(value).replace("\\", "/")
            previous = shlex.join([shell_path(sys.executable), shell_path(old), shell_path(observed)])
            settings = profile / "settings.json"
            original = {"statusLine": {"type": "command", "command": previous, "padding": 2}}
            settings.write_text(json.dumps(original), encoding="utf-8")
            full_path = ctypes.create_unicode_buffer(32768)
            ctypes.windll.kernel32.GetLongPathNameW(str(project), full_path, len(full_path))
            paths = {str(project), str(project.resolve()), full_path.value}
            paths |= {value.replace("\\", "/") for value in paths}
            paths |= {value.lower() for value in paths}
            state = {"hasCompletedOnboarding": True, "theme": "dark", "autoUpdates": False,
                     "customApiKeyResponses": {"approved": ["synthetic-local-key"], "rejected": []},
                     "projects": {value: {"hasTrustDialogAccepted": True,
                                          "hasCompletedProjectOnboarding": True} for value in paths if value}}
            (profile / ".claude.json").write_text(json.dumps(state), encoding="utf-8")
            store = root / "quota.sqlite3"
            configure(settings, "claude_code", store, enabled=True, windows_shell=("bash", bash))
            with ThreadingHTTPServer(("127.0.0.1", 0), FakeClaudeAPI) as server:
                server.paths = []
                service = Thread(target=server.serve_forever, daemon=True)
                service.start()
                env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT")
                       if key in os.environ}
                env.update({"USERPROFILE": str(root), "HOME": str(root), "APPDATA": str(root),
                            "LOCALAPPDATA": str(root), "TEMP": str(root), "TMP": str(root),
                            "CLAUDE_CONFIG_DIR": str(profile), "CLAUDE_CODE_GIT_BASH_PATH": bash,
                            "ANTHROPIC_API_KEY": "synthetic-local-key",
                            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                            "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "NO_PROXY": "127.0.0.1,localhost"})
                args = [str(cli), "--setting-sources", "", "--settings", str(settings), "--strict-mcp-config",
                        "--tools", "", "--model", "claude-sonnet-4-5-20250929", "Local fixture"]
                process = PtyProcess.spawn(args, cwd=str(project), env=env, dimensions=(36, 105))
                output = []
                def drain():
                    try:
                        while process.isalive():
                            output.append(process.read(8192))
                    except (EOFError, OSError):
                        pass
                reader = Thread(target=drain, daemon=True)
                reader.start()
                try:
                    deadline = time.monotonic() + 45
                    quota = QuotaStore(store)
                    trust_selected_at = None
                    trusted = False
                    while time.monotonic() < deadline and process.isalive():
                        if trust_selected_at is None and "Yes," in "".join(output) and "trust" in "".join(output):
                            process.write("\x1b[B")
                            trust_selected_at = time.monotonic()
                        elif trust_selected_at is not None and not trusted and time.monotonic() - trust_selected_at > .5:
                            process.write("\r")
                            trusted = True
                        if observed.is_file() and quota.read("claude_code", time.time()) is not None and server.paths:
                            break
                        time.sleep(.1)
                    self.assertTrue(observed.is_file(), "original statusline did not run; output=" + "".join(output)[-3000:])
                    self.assertTrue(json.loads(observed.read_text())["has_model"])
                    self.assertIsNotNone(quota.read("claude_code", time.time()))
                    self.assertTrue(any(path.startswith("/v1/messages") for path in server.paths), server.paths)
                    before_removal = json.loads(settings.read_text(encoding="utf-8"))
                    self.assertTrue(configure(settings, "claude_code", store, enabled=False, windows_shell=("bash", bash)))
                    restored = json.loads(settings.read_text(encoding="utf-8"))
                    self.assertEqual(restored["statusLine"], original["statusLine"])
                    before_removal.pop("statusLine")
                    restored.pop("statusLine")
                    self.assertEqual(restored, before_removal)
                finally:
                    process.close(force=True)
                    server.shutdown()
                    service.join(timeout=2)
