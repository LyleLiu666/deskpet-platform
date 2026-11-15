import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig

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
