import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from deskpet_platform.quota_capture import QuotaStore
from deskpet_platform.quota_statusline import configure


class QuotaStatuslineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="deskpet status with spaces ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = self.root / "agent" / "settings.json"
        self.store = self.root / "data dir" / "quota.sqlite3"
        self.settings.parent.mkdir()

    def write_settings(self, value):
        self.settings.write_text(json.dumps(value), encoding="utf-8")

    def settings_json(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def run_command(self, data):
        command = self.settings_json()["statusLine"]["command"]
        return subprocess.run(["/bin/sh", "-c", command], input=data, capture_output=True, timeout=5)

    def test_existing_statusline_receives_original_input_and_metadata_survives_round_trip(self):
        script = self.root / "previous status.py"
        script.write_text("import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(b'old:' + data); sys.stderr.write('old stderr')\n")
        original = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}; printf '\\nsecond line' # trailing comment"
        initial = {"other": {"keep": True}, "statusLine": {"type": "command", "command": original,
                                                               "padding": 3, "refreshInterval": 60}}
        self.write_settings(initial)
        self.assertTrue(configure(self.settings, "claude_code", self.store, enabled=True))
        self.assertFalse(configure(self.settings, "claude_code", self.store, enabled=True))
        now = int(time.time())
        data = json.dumps({"private": "do not save", "rate_limits": {"five_hour": {
            "used_percentage": 20, "resets_at": now + 3600}}}).encode()
        run = self.run_command(data)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout, b"old:" + data + b"\nsecond line")
        self.assertEqual(run.stderr, b"old stderr")
        self.assertEqual(QuotaStore(self.store).read("claude_code", time.time()), {"quotas": [[80, 300, now + 3600]]})
        self.assertNotIn(b"do not save", self.store.read_bytes())
        self.assertTrue(configure(self.settings, "claude_code", self.store, enabled=False))
        self.assertEqual(self.settings_json(), initial)
        self.assertFalse(configure(self.settings, "claude_code", self.store, enabled=False))

    def test_install_into_empty_agent_and_remove_preserves_other_edits(self):
        self.write_settings({"other": 1})
        self.assertTrue(configure(self.settings, "antigravity", self.store, enabled=True))
        installed = self.settings_json()
        self.assertTrue(installed["statusLine"]["stack_with_default"])
        run = self.run_command(b'{"quota":{"gemini-weekly":{"remaining_fraction":0.8}}}')
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, b"", b""))
        installed["other"] = 2
        self.write_settings(installed)
        self.assertTrue(configure(self.settings, "antigravity", self.store, enabled=False))
        self.assertEqual(self.settings_json(), {"other": 2})

    def test_user_edits_in_statusline_are_not_lost_on_removal(self):
        self.write_settings({"statusLine": {"type": "command", "command": "cat", "padding": 1}})
        configure(self.settings, "claude_code", self.store, enabled=True)
        config = self.settings_json()
        config["statusLine"]["padding"] = 4
        config["statusLine"]["newSetting"] = True
        self.write_settings(config)
        configure(self.settings, "claude_code", self.store, enabled=False)
        self.assertEqual(self.settings_json()["statusLine"], {"type": "command", "command": "cat", "padding": 4, "newSetting": True})

    def test_reinstall_for_a_new_location_does_not_wrap_the_wrapper(self):
        self.write_settings({"statusLine": {"type": "command", "command": "cat"}})
        configure(self.settings, "claude_code", self.store, enabled=True)
        other_store = self.root / "different.sqlite3"
        self.assertTrue(configure(self.settings, "claude_code", other_store, enabled=True))
        self.assertEqual(self.run_command(b'{}').stdout, b'{}')
        self.assertTrue(other_store.exists())
        configure(self.settings, "claude_code", other_store, enabled=False)
        self.assertEqual(self.settings_json()["statusLine"]["command"], "cat")

    def test_fails_open_when_capture_program_disappears(self):
        self.write_settings({"statusLine": {"type": "command", "command": "cat"}})
        configure(self.settings, "claude_code", self.store, enabled=True, executable=self.root / "missing python")
        result = self.run_command(b'{"private":"unchanged"}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'{"private":"unchanged"}', b''))

    def test_preserves_previous_exit_code_and_does_not_print_capture_errors(self):
        self.write_settings({"statusLine": {"type": "command", "command": "printf old; exit 7"}})
        configure(self.settings, "claude_code", self.root, enabled=True)
        result = self.run_command(b'{}')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b'old', b''))

    def test_refuses_disabled_or_unknown_statusline_without_changing_settings(self):
        for status in ({"enabled": False, "command": "cat"}, {"type": "unexpected", "command": "cat"}, "invalid"):
            with self.subTest(status=status):
                original = {"statusLine": status, "other": 3}
                self.write_settings(original)
                with self.assertRaises(ValueError):
                    configure(self.settings, "antigravity", self.store, enabled=True)
                self.assertEqual(self.settings_json(), original)

    def test_leaves_user_replaced_command_alone(self):
        self.write_settings({"statusLine": {"type": "command", "command": "cat"}})
        configure(self.settings, "claude_code", self.store, enabled=True)
        config = self.settings_json()
        config["statusLine"]["command"] = "printf user-new"
        self.write_settings(config)
        self.assertFalse(configure(self.settings, "claude_code", self.store, enabled=False))
        self.assertEqual(self.settings_json(), config)

    def test_invalid_or_symlinked_configuration_is_never_overwritten(self):
        self.settings.write_text("not-json")
        with self.assertRaises(ValueError):
            configure(self.settings, "claude_code", self.store, enabled=True)
        self.assertEqual(self.settings.read_text(), "not-json")
        target = self.root / "target"
        target.write_text('{}')
        self.settings.unlink()
        self.settings.symlink_to(target)
        with self.assertRaises(ValueError):
            configure(self.settings, "claude_code", self.store, enabled=True)
        self.assertEqual(target.read_text(), '{}')


if __name__ == "__main__":
    unittest.main()

class QuotaStatuslineMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_configures_and_removes_claude_capture_without_a_board(self):
        import asyncio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="deskpet-mcp-agent-") as directory:
            root = Path(directory)
            agent = root / "claude"
            agent.mkdir()
            settings = agent / "settings.json"
            original = {"statusLine": {"type": "command", "command": "cat", "padding": 2}, "other": True}
            settings.write_text(json.dumps(original))
            command = StdioServerParameters(command=sys.executable, args=["-m", "deskpet_platform", "--config",
                str(root / "device.json"), "mcp", "--no-token-probe"], env={"CLAUDE_CONFIG_DIR": str(agent)})
            async with asyncio.timeout(10):
                async with stdio_client(command) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        enabled = await session.call_tool("desk_set_agent_quota", {"source": "claude_code", "enabled": True})
                        self.assertFalse(enabled.isError, enabled)
                        self.assertTrue(enabled.structuredContent["changed"])
                        input_bytes = b'{"rate_limits":{},"private":"unchanged"}'
                        status_command = json.loads(settings.read_text())["statusLine"]["command"]
                        run = subprocess.run(["/bin/sh", "-c", status_command], input=input_bytes, capture_output=True)
                        self.assertEqual(run.stdout, input_bytes)
                        self.assertEqual(run.returncode, 0)
                        self.assertTrue((root / "agent-quota.sqlite3").exists())
                        again = await session.call_tool("desk_set_agent_quota", {"source": "claude_code", "enabled": True})
                        self.assertFalse(again.structuredContent["changed"])
                        disabled = await session.call_tool("desk_set_agent_quota", {"source": "claude_code", "enabled": False})
                        self.assertFalse(disabled.isError, disabled)
                        self.assertEqual(json.loads(settings.read_text()), original)
