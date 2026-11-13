import base64
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from deskpet_platform.__main__ import interact
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.pairing import DeviceConfig

KEY = bytes(range(32))
FIRST = DeviceConfig('02:00:00:00:00:01', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f10')
SECOND = DeviceConfig('02:00:00:00:00:02', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f11')


def code(config):
    return 'D1-' + config.identity.replace(':', '') + '-' + base64.urlsafe_b64encode(KEY).decode().rstrip('=')


class MultiDeviceCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_rejects_replacing_a_different_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            FIRST.save(path)
            args = SimpleNamespace(command='pair', config=path, address=None, add_device=False)
            with patch('deskpet_platform.__main__.getpass', return_value=code(SECOND)), \
                 patch('deskpet_platform.client.Client') as client:
                with self.assertRaises(ValueError):
                    await interact(args)
                client.assert_not_called()
            self.assertEqual(DeviceConfig.load(path), FIRST)

    async def test_add_pairing_keeps_existing_config_and_records_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            FIRST.save(path)
            board = AsyncMock()
            board.__aenter__.return_value = board
            board.device_id = SECOND.device_id
            board.channel.capabilities = {}
            args = SimpleNamespace(command='pair', config=path, address=None, add_device=True)
            with patch('deskpet_platform.__main__.getpass', return_value=code(SECOND)), \
                 patch('deskpet_platform.client.Client', return_value=board), \
                 patch('deskpet_platform.mcp_server.status_view', return_value={}):
                await interact(args)
            self.assertEqual(DeviceConfig.load(path), FIRST)
            self.assertEqual(DeviceRegistry(path).get(SECOND.device_id), SECOND)

    async def test_status_requires_explicit_selection_for_multiple_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            FIRST.save(path)
            DeviceRegistry(path).add(SECOND)
            args = SimpleNamespace(command='status', config=path, device_id=None)
            with patch('deskpet_platform.client.Client') as client:
                with self.assertRaises(ValueError):
                    await interact(args)
                client.assert_not_called()

    async def test_status_connects_only_to_the_selected_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            FIRST.save(path)
            DeviceRegistry(path).add(SECOND)
            args = SimpleNamespace(command='status', config=path, device_id=SECOND.device_id)
            board = AsyncMock()
            board.__aenter__.return_value = board
            board.channel.capabilities = {}
            with patch('deskpet_platform.client.Client', return_value=board) as client, \
                 patch('deskpet_platform.mcp_server.status_view', return_value={'connection': 'connected'}):
                self.assertEqual(await interact(args), {'connection': 'connected'})
            client.assert_called_once_with(SECOND)
