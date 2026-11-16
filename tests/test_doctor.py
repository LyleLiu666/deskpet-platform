import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import antigravity_feedback, opencode_feedback
from deskpet_platform.codex_hook import configure_codex_hooks, hook_command, install_hook
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.doctor import diagnose, host_platform
from deskpet_platform.notifications import NotificationStore
from deskpet_platform.pairing import DeviceConfig


class DoctorTests(unittest.TestCase):
    def test_existing_codex_hook_from_old_install_is_reported_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "device.json"
            path = root / "hooks.json"
            executable = root / "old install" / ("python.exe" if os.name == "nt" else "python3.13")
            with patch.object(sys, "executable", str(executable)):
                old = hook_command(config)
            configure_codex_hooks(path, old, [], enabled=True)
            configure_codex_hooks(path, hook_command(root / "other.json"), [], enabled=True)
            original = path.read_bytes()

            with patch.dict(os.environ, CODEX_HOME=str(root)):
                report = diagnose(config, client="codex")
                self.assertEqual(report["reminders"]["hook"]["status"], "needs_reinstall")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(diagnose(root / "other.json", client="codex")["reminders"]["hook"]["status"], "written")
                install_hook(path, hook_command(config))
                self.assertEqual(diagnose(config, client="codex")["reminders"]["hook"]["status"], "needs_reinstall")
                configure_codex_hooks(path, hook_command(config), [], enabled=True)
                self.assertEqual(diagnose(config, client="codex")["reminders"]["hook"]["status"], "written")

    @unittest.skipIf(os.name == "nt", "These notification hooks currently require POSIX")
    def test_previous_install_is_found_in_antigravity_and_opencode(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory):
            root = Path(directory)
            config = root / "device.json"
            path = root / "antigravity-hooks.json"
            prefix = ["/old install/python3", "-m", "deskpet_platform", "--config", str(config)]
            antigravity = shlex.join([*prefix, "antigravity-hook"])
            antigravity_feedback.configure(path, antigravity, enabled=True)
            with patch.object(antigravity_feedback, "settings_path", return_value=path):
                result = diagnose(config, client="antigravity")
            self.assertEqual(result["reminders"]["hook"]["status"], "needs_reinstall")

            opencode = shlex.join([*prefix, "opencode-hook"])
            old_plugin = opencode_feedback.plugin_path(opencode)
            opencode_feedback.configure(old_plugin, opencode, enabled=True)
            result = diagnose(config, client="opencode")
            self.assertEqual(result["reminders"]["hook"]["status"], "needs_reinstall")

            # A user-edited plugin is outside the scope of automatic migration.
            old_plugin.write_bytes(old_plugin.read_bytes() + b"// custom\n")
            result = diagnose(config, client="opencode")
            self.assertEqual(result["reminders"]["hook"]["status"], "missing")

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
                self.assertIsNone(report["reminders"]["latest_events"]["completed"]["device_displayed_at"])
                device.mark_home_displayed([new])
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
