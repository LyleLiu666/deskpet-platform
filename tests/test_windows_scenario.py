import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "nt", "Windows onboarding rehearsal")
class WindowsScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_registry_mcp_and_private_agent_state(self):
        from deskpet_platform.device_registry import DeviceRegistry
        from deskpet_platform.mcp_server import create_server
        from deskpet_platform.notifications import NotificationStore
        from deskpet_platform.pairing import DeviceConfig
        from deskpet_platform.quota_capture import QuotaStore
        from deskpet_platform.usb_link import valid_usb_port, windows_port_identity
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="DeskPet user with spaces ") as directory:
            root = Path(directory)
            path = root / "device.json"
            config = DeviceConfig("02:00:00:00:00:01", bytes(range(32)), device_id="00000000-0000-4000-8000-000000000001")
            registry = DeviceRegistry(path)
            registry.save_default(config, None)
            self.assertEqual(registry.get(config.device_id), config)
            self.assertEqual(registry.list()[0]["id"], config.device_id)
            server = create_server(path, no_token_probe=True)
            result = await server.call_tool("desk_list_devices", {})
            self.assertEqual(result[1]["devices"][0]["id"], config.device_id)
            notices = NotificationStore(root / "notifications.sqlite3")
            notices.add_agent_event("claude_code", "synthetic-session", "synthetic-turn", "completed", "Fixture")
            self.assertEqual(notices.list()[0]["title"], "Fixture")
            self.assertEqual(notices.path.parent.name, ".deskpet-private")
            quotas = QuotaStore(root / "agent-quota.sqlite3")
            now = int(time.time())
            quotas.record("claude_code", {"rate_limits":{"five_hour":{"used_percentage":20,"resets_at":now+600}}}, now)
            self.assertEqual(quotas.read("claude_code", now), {"quotas":[[80,300,now+600]]})
            self.assertEqual(quotas.path.parent, notices.path.parent)
            self.assertTrue(valid_usb_port("COM12"))
            adapter = SimpleNamespace(device="COM12",vid=1,pid=2,serial_number="fixture",location="1-1",hwid="fixture")
            self.assertEqual(windows_port_identity("com12",[adapter])[2], "fixture")

    async def test_codex_shim_path_rejects_command_interpreters_metacharacters(self):
        from unittest.mock import patch
        from deskpet_platform.codex_quota import fetch_quota
        for character in ("&", "|", "<", ">", "^", "(", ")", "%"):
            with self.subTest(character=character), patch("deskpet_platform.codex_quota.shutil.which", return_value=f"C:\\test{character}bad\\codex.cmd"):
                with self.assertRaises(ValueError):
                    await fetch_quota()

    async def test_codex_quota_can_start_a_windows_command_shim(self):
        import json
        import sys
        from unittest.mock import patch
        from deskpet_platform.codex_quota import fetch_quota

        with tempfile.TemporaryDirectory(prefix="DeskPet codex with spaces ") as directory:
            root = Path(directory)
            fixture = root / "fake_codex.py"
            fixture.write_text('''import json,sys,time
for line in sys.stdin:
    message=json.loads(line)
    if message.get("method")=="initialize":
        print(json.dumps({"id":message["id"],"result":{"protocolVersion":"test"}}),flush=True)
    elif message.get("method")=="account/rateLimits/read":
        print(json.dumps({"id":message["id"],"result":{"rateLimitsByLimitId":{"codex":{"primary":{"windowDurationMins":300,"usedPercent":20,"resetsAt":int(time.time())+600}}}}}),flush=True)
''', encoding="utf-8")
            shim = root / "codex.cmd"
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{fixture}" %*\r\n', encoding="utf-8")
            with patch("deskpet_platform.codex_quota.shutil.which", return_value=str(shim)):
                reading = await fetch_quota()
            self.assertEqual(reading["quotas"][0][:2], [80, 300])
