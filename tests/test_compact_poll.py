import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from deskpet_platform.channel import AckTimeout
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.notifications import NotificationStore

CONFIG = DeviceConfig('02:00:00:00:00:01', bytes(range(32)))


class CompactPollTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_delivery_waits_for_visible_text_page(self):
        for capabilities, page, expected in (({'text_present': 1}, 255, False),
                                              ({}, 0, False),
                                              ({'text_present': 1}, 0, True)):
            with self.subTest(capabilities=capabilities, page=page), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'device.json'
                CONFIG.save(path)
                session = BoardSession(path)
                board = AsyncMock()
                board.device_id = None
                board.link.wireless = True
                board.channel.capabilities = capabilities
                board.status.return_value = {'boot': 1, 'pet_page': page}
                session.connect = AsyncMock(return_value=board)
                session.recover_wifi = AsyncMock()
                deliver = AsyncMock()
                with patch('deskpet_platform.mcp_server.deliver_next', deliver), \
                     patch('deskpet_platform.mcp_server.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await session.poll()
                self.assertEqual(deliver.await_count, int(expected))

    async def test_local_notice_storage_error_keeps_link_online_but_link_failure_drops_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            CONFIG.save(path)
            session = BoardSession(path)
            blocked = path.parent / "blocked"
            blocked.write_text("not a directory")
            session.notifications = NotificationStore(blocked / "notifications.sqlite3")
            board = AsyncMock()
            board.device_id = None
            board.link.wireless = True
            board.channel.capabilities = {"notices": 1}
            board.status.return_value = {"boot": 1}
            session.connect = AsyncMock(return_value=board)
            session.recover_wifi = AsyncMock()
            with patch("deskpet_platform.mcp_server.asyncio.sleep",
                       AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
                self.assertIs(session.client, board)
                self.assertEqual(session.snapshot["connection"], "connected")
                self.assertIn("notification_error", session.snapshot)
                board.close.assert_not_awaited()

                board.status.side_effect = FileNotFoundError("USB adapter disconnected")
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
                self.assertEqual(session.snapshot["connection"], "offline")
                board.close.assert_awaited_once()

    async def test_disconnect_evidence_survives_failed_retries_and_records_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            board = AsyncMock()
            board.link.wireless = True
            board.channel.capabilities = {}
            session.client = board
            session.record_status({"boot": 1})
            with patch("deskpet_platform.mcp_server.time.monotonic", return_value=10):
                await session.record_link_error(AckTimeout("heartbeat did not reply"))
            with patch("deskpet_platform.mcp_server.time.monotonic", return_value=12):
                await session.record_link_error(OSError("wifi_device_absent"))
            evidence = (await session.get_status())["last_link_loss"]
            self.assertEqual(evidence["reason"], "ack_timeout")
            self.assertNotIn("connection_recovery_ms", evidence)
            session.client = board
            board.status.return_value = {"boot": 1}
            with patch("deskpet_platform.mcp_server.time.monotonic", return_value=13.25):
                refreshed = await session.get_status()
            self.assertEqual(refreshed["connection"], "connected")
            self.assertEqual(refreshed["last_link_loss"]["observed_at"], evidence["observed_at"])
            self.assertEqual(refreshed["last_link_loss"]["connection_recovery_ms"], 3250)

    def make_status_session(self, directory, *, wireless=True):
        path = Path(directory) / "device.json"
        CONFIG.save(path)
        session = BoardSession(path)
        client = self.make_status_client(wireless=wireless)
        client.status.side_effect = AckTimeout("heartbeat did not reply")
        session.client = client
        session.record_status({"boot": 1})
        return session, client

    def make_status_client(self, *, wireless=True, boot=1):
        client = AsyncMock(config=CONFIG, device_id=None)
        client.link.wireless = wireless
        client.channel.boot = boot
        client.channel.capabilities = {"clock_sync": 1}
        client.channel.dropped_events = 0
        return client

    async def test_explicit_status_recovers_wifi_once(self):
        with tempfile.TemporaryDirectory() as directory:
            session, timed_out = self.make_status_session(directory)
            replacement = self.make_status_client()
            replacement.status.return_value = {"boot": 1, "pet_page": 3}
            session.usb_port = Mock()
            session.last_notice_count = 1
            session.last_message_cache = {"pending": 1}
            session.snapshot["notification_error"] = "fixture notice error"

            async def reconnect(config):
                timed_out.close.assert_awaited_once()
                self.assertIsNone(session.client)
                return replacement

            with patch.object(session, "open_wifi", side_effect=reconnect) as connect, \
                 patch.object(session, "usb_client") as usb:
                status = await session.get_status()

            connect.assert_awaited_once_with(CONFIG)
            usb.assert_not_called()
            timed_out.status.assert_awaited_once_with()
            replacement.status.assert_awaited_once_with()
            replacement.sync_clock.assert_not_awaited()
            self.assertIs(session.client, replacement)
            self.assertIsNone(session.last_notice_count)
            self.assertIsNone(session.last_message_cache)
            self.assertEqual(status["connection"], "connected")
            self.assertEqual(status["page"], 3)
            self.assertEqual(status["notification_error"], "fixture notice error")
            self.assertEqual(status["last_link_loss"]["reason"], "ack_timeout")
            self.assertIn("connection_recovery_ms", status["last_link_loss"])

    async def test_explicit_status_failed_wifi_recovery_closes_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            session, timed_out = self.make_status_session(directory)
            replacement = self.make_status_client()
            replacement.status.side_effect = AckTimeout("second heartbeat did not reply")
            with patch.object(session, "open_wifi", return_value=replacement) as connect:
                status = await session.get_status()
                self.assertEqual(await session.get_status(), status)

            connect.assert_awaited_once_with(CONFIG)
            timed_out.close.assert_awaited_once()
            replacement.status.assert_awaited_once_with()
            replacement.close.assert_awaited_once()
            self.assertEqual(status["connection"], "offline")
            self.assertEqual(status["last_link_loss"]["reason"], "ack_timeout")
            self.assertNotIn("connection_recovery_ms", status["last_link_loss"])

    async def test_explicit_status_recovers_wifi_stream_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            session, disconnected = self.make_status_session(directory)
            disconnected.status.side_effect = OSError("wifi_disconnected: wifi_disconnected")
            replacement = self.make_status_client()
            replacement.status.return_value = {"boot": 1, "pet_page": 3}
            with patch.object(session, "open_wifi", return_value=replacement) as connect:
                status = await session.get_status()

            connect.assert_awaited_once_with(CONFIG)
            disconnected.close.assert_awaited_once()
            replacement.status.assert_awaited_once_with()
            self.assertEqual(status["connection"], "connected")
            self.assertEqual(status["page"], 3)
            self.assertEqual(status["last_link_loss"]["reason"], "wifi_stream_closed")

    async def test_unrelated_wifi_status_error_does_not_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            session, disconnected = self.make_status_session(directory)
            disconnected.status.side_effect = OSError("fixture unrelated error")
            with patch.object(session, "open_wifi") as connect:
                status = await session.get_status()

            connect.assert_not_called()
            disconnected.close.assert_awaited_once()
            self.assertEqual(status["connection"], "offline")
            self.assertEqual(status["last_link_loss"]["reason"], "link_error")

    async def test_explicit_status_wifi_recovery_has_a_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            session, _timed_out = self.make_status_session(directory)
            replacement = self.make_status_client()
            replacement.status.side_effect = asyncio.Event().wait
            with patch.object(session, "open_wifi", return_value=replacement) as connect, \
                 patch("deskpet_platform.mcp_server.STATUS_WIFI_RECOVERY_TIMEOUT", .05):
                status = await asyncio.wait_for(session.get_status(), 1)

            connect.assert_awaited_once_with(CONFIG)
            replacement.close.assert_awaited_once()
            self.assertIsNone(session.client)
            self.assertEqual(status["connection"], "offline")

    async def test_explicit_status_usb_timeout_does_not_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            session, timed_out = self.make_status_session(directory, wireless=False)
            with patch.object(session, "open_wifi") as connect:
                status = await session.get_status()

            connect.assert_not_called()
            timed_out.close.assert_awaited_once()
            self.assertEqual(status["connection"], "offline")
            self.assertEqual(status["last_link_loss"]["reason"], "ack_timeout")

    async def test_explicit_status_wifi_recovery_leaves_new_boot_for_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            session, _timed_out = self.make_status_session(directory)
            replacement = self.make_status_client(boot=2)
            with patch.object(session, "open_wifi", return_value=replacement) as connect:
                status = await session.get_status()

            connect.assert_awaited_once_with(CONFIG)
            replacement.status.assert_not_awaited()
            replacement.close.assert_awaited_once()
            self.assertIsNone(session.client)
            self.assertEqual(status["connection"], "offline")

    async def test_explicit_status_wifi_recovery_rejects_changed_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            session, _timed_out = self.make_status_session(directory)
            replacement = self.make_status_client()

            async def status_after_pairing_change():
                DeviceConfig("02:00:00:00:00:02", bytes(range(32))).save(session.path)
                return {"boot": 1}

            replacement.status.side_effect = status_after_pairing_change
            with patch.object(session, "open_wifi", return_value=replacement) as connect:
                status = await session.get_status()

            connect.assert_awaited_once_with(CONFIG)
            replacement.close.assert_awaited_once()
            self.assertIsNone(session.client)
            self.assertEqual(status["connection"], "offline")

    async def test_speed_recovery_requires_a_successful_device_update(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Mock(metric_version="fixture-output-v1", speed_state="L")
            probe.sample = AsyncMock(return_value={"rate10": 120, "sessions": 1, "trend": [0] * 12, "quotas": []})
            session = BoardSession(Path(directory) / "device.json", probe_collector=probe)
            board = AsyncMock()
            board.link.wireless = True
            board.channel.capabilities = {"token_stats": 1}
            session.client = board
            session.record_status({"boot": 1})
            session.token_snapshot = {"rate10": 120}
            session.token_published_at = time.monotonic()
            await session.record_link_error(OSError("wifi_disconnected"))
            session.client = board
            session.record_status({"boot": 1})

            board.update_tokens.side_effect = [OSError("device did not accept the sample"), None]
            real_sleep = asyncio.sleep
            async def stop_after_publish(_delay):
                await real_sleep(.02)
                raise asyncio.CancelledError
            with patch("deskpet_platform.mcp_server.asyncio.sleep", side_effect=stop_after_publish):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll_tokens()
                self.assertNotIn("speed_recovery_ms", session.snapshot["last_link_loss"])
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll_tokens()
            evidence = session.snapshot["last_link_loss"]
            self.assertGreaterEqual(evidence["speed_recovery_ms"], evidence["connection_recovery_ms"])
            self.assertEqual(board.update_tokens.await_count, 2)

    async def test_client_requests_compact_only_when_advertised(self):
        client = Client(CONFIG)
        client.channel = AsyncMock()
        client.channel.capabilities = {'compact_status': 1}
        await client.status(compact=True)
        client.channel.send.assert_awaited_once_with('heartbeat', compact=True)
        client.channel.reset_mock()
        client.channel.capabilities = {}
        await client.status(compact=True)
        client.channel.send.assert_awaited_once_with('heartbeat')



    async def test_background_uses_compact_wifi_but_full_status_stays_available(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            CONFIG.save(path)
            session = BoardSession(path)
            board = AsyncMock()
            board.device_id = None
            board.link.wireless = True
            board.channel.capabilities = {'compact_status': 1, 'speed_windows': 1}
            board.status.return_value = {
                'boot': 1, 'pet_page': 0, 'role_state': 'ready',
                'speed_windows': {'recent_seconds': 1800, 'long_days': 7},
            }
            session.connect = AsyncMock(return_value=board)
            session.recover_wifi = AsyncMock()
            with patch('deskpet_platform.mcp_server.deliver_next', AsyncMock()), \
                 patch('deskpet_platform.mcp_server.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
            board.status.assert_awaited_once_with(compact=True)
            self.assertEqual(session.snapshot['speed_windows']['recent_seconds'], 1800)
            self.assertEqual(session.snapshot['role']['state'], 'ready')
            board.status.reset_mock()
            await session.get_status()
            board.status.assert_awaited_once_with()

    async def test_old_firmware_and_usb_keep_using_full_status(self):
        for wireless, capabilities in ((True, {}), (False, {'compact_status': 1})):
            with self.subTest(wireless=wireless, capabilities=capabilities), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'device.json'
                CONFIG.save(path)
                session = BoardSession(path)
                board = AsyncMock()
                board.device_id = None
                board.link.wireless = wireless
                board.channel.capabilities = capabilities
                board.status.return_value = {'boot': 1}
                session.connect = AsyncMock(return_value=board)
                session.recover_wifi = AsyncMock()
                with patch('deskpet_platform.mcp_server.deliver_next', AsyncMock()), \
                     patch('deskpet_platform.mcp_server.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await session.poll()
                board.status.assert_awaited_once_with()

    async def test_compact_poll_refreshes_windows_and_delivers_touch_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            CONFIG.save(path)
            session = BoardSession(path)
            board = AsyncMock()
            board.device_id = None
            board.link.wireless = True
            board.channel.capabilities = {'compact_status': 1, 'speed_windows': 1, 'touch_demo': 1}
            board.status.return_value = {
                'boot': 1, 'pet_page': 0, 'role_state': 'ready',
                'speed_windows': {'recent_seconds': 300, 'long_days': 3},
            }
            event = {'action': 'tap', 'id': 'screen'}
            board.pending_touch.side_effect = [event, None]
            session.connect = AsyncMock(return_value=board)
            session.recover_wifi = AsyncMock()
            session.process_touch = AsyncMock()
            session.snapshot['speed_windows'] = {'recent_seconds': 1800, 'long_days': 7}
            session.token_snapshot = {'stale': True}
            with patch('deskpet_platform.mcp_server.deliver_next', AsyncMock()), \
                 patch('deskpet_platform.mcp_server.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
            self.assertEqual(session.snapshot['speed_windows'], {'recent_seconds': 300, 'long_days': 3})
            self.assertIsNone(session.token_snapshot)
            session.process_touch.assert_awaited_once_with(event)
