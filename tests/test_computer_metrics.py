from collections import namedtuple
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from deskpet_platform.computer_metrics import ComputerMetrics, DisplayPreferences
from deskpet_platform.client import Client as BoardClient
from deskpet_platform.mcp_server import BoardSession, status_view
from deskpet_platform.pairing import DeviceConfig


class ComputerMetricsTests(unittest.TestCase):
    def test_status_reports_only_a_valid_weekly_reset_time(self):
        self.assertEqual(status_view({"weekly_reset": 1900000100}, {})["weekly_reset_at"], 1900000100)
        self.assertNotIn("weekly_reset_at", status_view({"weekly_reset": -1}, {}))

    def test_disabled_metrics_do_not_read_the_computer(self):
        class Source:
            def cpu_times(self): raise AssertionError("CPU was not requested")
            def virtual_memory(self): raise AssertionError("memory was not requested")
        self.assertEqual(ComputerMetrics(Source()).sample(cpu=False, memory=False), {})

    def test_cpu_uses_a_time_delta_and_optional_fields_do_not_leak(self):
        Times = namedtuple("Times", "user system idle iowait")
        class Source:
            rows = iter((Times(100, 50, 800, 50), Times(105, 55, 880, 60),
                         Times(200, 70, 900, 60), Times(205, 75, 980, 70)))
            def cpu_times(self): return next(self.rows)
            def virtual_memory(self): return type("Memory", (), {"percent": 63.6})()
        source = Source()
        metrics = ComputerMetrics(source)
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": None})
        self.assertEqual(metrics.sample(cpu=True, memory=True), {"cpu": 10, "memory": 64})
        self.assertEqual(metrics.sample(cpu=False, memory=True), {"memory": 64})
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": None})
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": 10})

    def test_counter_resets_and_unavailable_memory_are_unknown(self):
        Times = namedtuple("Times", "user system idle")
        class Source:
            rows = iter((Times(100, 50, 800), Times(1, 1, 1)))
            def cpu_times(self): return next(self.rows)
            def virtual_memory(self): raise OSError("unavailable")
        metrics = ComputerMetrics(Source())
        self.assertEqual(metrics.sample(cpu=True, memory=True), {"cpu": None, "memory": None})
        self.assertEqual(metrics.sample(cpu=True, memory=True), {"cpu": None, "memory": None})

    def test_long_sampling_gap_restarts_cpu_measurement(self):
        Times = namedtuple("Times", "user system idle")
        class Source:
            rows = iter((Times(10, 10, 80), Times(20, 20, 160), Times(21, 21, 168)))
            def cpu_times(self): return next(self.rows)
        times = iter((0, 30, 31))
        metrics = ComputerMetrics(Source(), clock=lambda: next(times))
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": None})
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": None})
        self.assertEqual(metrics.sample(cpu=True, memory=False), {"cpu": 20})

    def test_preferences_default_off_and_persist_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            first = DisplayPreferences(Path(directory) / "one/settings.json")
            second = DisplayPreferences(Path(directory) / "two/settings.json")
            self.assertEqual(first.get(), {"cpu": False, "memory": False})
            first.update(cpu=True)
            self.assertEqual(first.get(), {"cpu": True, "memory": False})
            self.assertEqual(DisplayPreferences(first.path).get(), first.get())
            self.assertEqual(second.get(), {"cpu": False, "memory": False})
            first.update(memory=True)
            self.assertEqual(DisplayPreferences(first.path).get(), {"cpu": True, "memory": True})
            self.assertEqual(first.path.stat().st_mode & 0o077, 0)
            with self.assertRaises(ValueError): first.update(cpu="yes")
            self.assertEqual(first.get(), {"cpu": True, "memory": True})

    def test_old_unsent_preferences_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computer-display.json"
            path.write_text(json.dumps({"cpu": True, "memory": False, "pending_device": True}))
            saved = DisplayPreferences(path)
            self.assertEqual(saved.get(), {"cpu": True, "memory": False})
            self.assertEqual(saved.pending_fields, {"cpu", "memory"})


class ComputerMetricsSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_loss_between_acknowledgement_and_write_keeps_queued_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            session = BoardSession(path)
            await session.configure_computer_metrics(cpu=True)
            client = SimpleNamespace(channel=SimpleNamespace(capabilities={"computer_preferences": 1}),
                                     acknowledge_computer_preferences=AsyncMock(),
                                     set_computer_preferences=AsyncMock(side_effect=OSError("offline")))
            with self.assertRaises(OSError):
                await session.sync_computer_preferences(client, {"computer_prefs": 14})
            restored = BoardSession(path)
            self.assertEqual(restored.computer_display.pending_fields, {"cpu"})
            client.set_computer_preferences.side_effect = None
            await restored.sync_computer_preferences(client, {"computer_prefs": 10})
            client.set_computer_preferences.assert_awaited_with(cpu_enabled=True, memory_enabled=True)
            self.assertFalse(restored.computer_display.pending_fields)

    async def test_reconnect_combines_disconnected_changes_to_separate_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            offline = BoardSession(path)
            await offline.configure_computer_metrics(cpu=True)
            restored = BoardSession(path)
            client = SimpleNamespace(channel=SimpleNamespace(capabilities={"computer_preferences": 1}),
                                     acknowledge_computer_preferences=AsyncMock(),
                                     set_computer_preferences=AsyncMock())
            restored.client = client
            await restored.sync_computer_preferences(client, {"computer_prefs": 14})
            client.acknowledge_computer_preferences.assert_awaited_once_with(14)
            client.set_computer_preferences.assert_awaited_once_with(cpu_enabled=True, memory_enabled=True)
            self.assertEqual(DisplayPreferences(restored.computer_display.path).get(), {"cpu": True, "memory": True})

    async def test_device_toggle_is_saved_per_device_and_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            first = BoardSession(Path(directory) / "first/device.json")
            second = BoardSession(Path(directory) / "second/device.json")
            client = SimpleNamespace(channel=SimpleNamespace(capabilities={"computer_preferences": 1}),
                                     acknowledge_computer_preferences=AsyncMock(return_value={"ok": True}))
            first.client = client
            # CPU enabled, memory disabled, device change awaiting acknowledgement.
            await first.sync_computer_preferences(client, {"computer_prefs": 13})
            self.assertEqual(first.computer_display.get(), {"cpu": True, "memory": False})
            self.assertEqual(DisplayPreferences(first.computer_display.path).get(), first.computer_display.get())
            self.assertEqual(second.computer_display.get(), {"cpu": False, "memory": False})
            client.acknowledge_computer_preferences.assert_awaited_once_with(13)
            self.assertIs(first.computer_preference_client, client)

    async def test_offline_computer_change_is_saved_and_applied_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            first = BoardSession(path)
            await first.configure_computer_metrics(cpu=True)
            restored = BoardSession(path)
            client = SimpleNamespace(channel=SimpleNamespace(capabilities={"computer_preferences": 1}),
                                     set_computer_preferences=AsyncMock(return_value={"ok": True}))
            restored.client = client
            await restored.sync_computer_preferences(client, {"computer_prefs": 0})
            client.set_computer_preferences.assert_awaited_once_with(cpu_enabled=True, memory_enabled=False)
            self.assertFalse(restored.computer_display.pending_fields)

    async def test_client_sends_bounded_metrics_and_rejects_invalid_values(self):
        class Channel:
            capabilities = {"computer_metrics": 1}
            async def send(self, kind, **fields):
                self.kind, self.fields = kind, fields
                return {"ok": True}
        client = BoardClient(DeviceConfig("02:00:00:00:00:01", bytes(32)), link_factory=lambda *_a, **_k: None)
        client.channel = channel = Channel()
        await client.update_computer_metrics(cpu_enabled=True, memory_enabled=False, cpu=12, memory=None)
        self.assertEqual(channel.kind, "computer_metrics")
        self.assertEqual(channel.fields, {"cpu_enabled": True, "memory_enabled": False, "cpu": 12, "memory": None})
        with self.assertRaises(ValueError):
            await client.update_computer_metrics(cpu_enabled=False, memory_enabled=True, cpu=10, memory=101)

    async def test_settings_are_independent_and_disabled_fields_are_cleared(self):
        class Channel:
            capabilities = {"computer_metrics": 1}
        class Client:
            channel = Channel()
            def __init__(self): self.sent = [];self.changed = asyncio.Event()
            async def update_computer_metrics(self, **fields):
                self.sent.append(fields);self.changed.set()
        class Sampler:
            def sample(self, *, cpu, memory):
                return {key: value for key, enabled, value in (("cpu", cpu, 23), ("memory", memory, 64)) if enabled}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = BoardSession(root / "one/device.json")
            two = BoardSession(root / "two/device.json")
            one.computer_metrics = Sampler()
            one.client = client = Client()
            self.assertEqual((await one.configure_computer_metrics(cpu=True))["enabled"], {"cpu": True, "memory": False})
            self.assertEqual((await two.configure_computer_metrics())["enabled"], {"cpu": False, "memory": False})
            task = asyncio.create_task(one.poll_computer_metrics())
            try:
                await asyncio.wait_for(client.changed.wait(), 3)
                self.assertEqual(client.sent[-1], {"cpu_enabled": True, "memory_enabled": False, "cpu": 23, "memory": None})
                client.changed.clear()
                await one.configure_computer_metrics(cpu=False, memory=True)
                await asyncio.wait_for(client.changed.wait(), 3)
                self.assertEqual(client.sent[-1], {"cpu_enabled": False, "memory_enabled": True, "cpu": None, "memory": 64})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
