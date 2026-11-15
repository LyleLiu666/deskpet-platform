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
