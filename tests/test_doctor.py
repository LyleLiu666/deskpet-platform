import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.codex_hook import configure_codex_hooks, hook_command
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.doctor import diagnose, host_platform
from deskpet_platform.notifications import NotificationStore
from deskpet_platform.pairing import DeviceConfig


class DoctorTests(unittest.TestCase):
    def test_new_install_leaves_pairing_and_agent_configuration_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, CODEX_HOME=str(root / "agent")):
                report = diagnose(root / "pairing" / "device.json", client="codex")
            self.assertEqual(report["pairing"]["devices"], [])
            self.assertEqual(report["reminders"]["hook"]["status"], "missing")
            self.assertEqual(list(root.iterdir()), [])

    def test_receipt_is_for_the_selected_device_and_comes_from_a_real_display(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "device.json"
            DeviceConfig("02:00:00:00:00:01", bytes(32)).save(shared)
            home = root / "agent"
            configure_codex_hooks(home / "hooks.json", hook_command(shared), [], enabled=True)
            incoming = NotificationStore(root / "notifications.sqlite3")
            event = incoming.add_agent_event("codex", "synthetic-session", "first-turn", "completed", "Private title")
            incoming.mark_shown(event)
            receiver = DeviceRegistry(shared).add_receiver(DeviceConfig("02:00:00:00:00:02", bytes(32)))
            device = NotificationStore(receiver.parent / "notifications.sqlite3", source=incoming)
            with patch.dict(os.environ, CODEX_HOME=str(home)):
                report = diagnose(receiver, client="codex", shared_config=shared, no_token_probe=True)
                self.assertEqual(report["reminders"]["hook"]["status"], "written")
                self.assertEqual(report["speed"]["status"], "disabled")
                self.assertEqual(report["pairing"]["connection"], "not_checked")
                self.assertTrue(device.list()[0]["shown"])
                self.assertIsNone(report["reminders"]["latest_events"]["completed"]["device_displayed_at"])

                incoming.add_agent_event("codex", "synthetic-session", "new-turn", "completed", "Private title")
                device.sync()
                new = device.list()[0]["id"]
                device.mark_home_shown([new])
                report = diagnose(receiver, client="codex", shared_config=shared)
            evidence = report["reminders"]["latest_events"]["completed"]
            self.assertGreaterEqual(evidence["device_displayed_at"], evidence["hook_triggered_at"])
            self.assertNotIn("Private title", str(report))

    def test_linux_musl_is_not_offered_a_glibc_probe(self):
        with patch("platform.system", return_value="Linux"), patch("platform.machine", return_value="x86_64"):
            with patch("platform.libc_ver", return_value=("glibc", "2.36")):
                self.assertEqual(host_platform()["release_probe_asset"], "AgentSpeedProbe-linux-x64.tar.gz")
            with patch("platform.libc_ver", return_value=("", "")):
                self.assertIsNone(host_platform()["release_probe_asset"])
