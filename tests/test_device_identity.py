import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from deskpet_platform.channel import DeviceError
from deskpet_platform.client import Client
from deskpet_platform.pairing import DeviceConfig

MAC='02:00:00:00:00:01'
KEY=bytes(range(32))
UUID='01020304-0506-4708-890a-0b0c0d0e0f10'
OTHER='01020304-0506-4708-890a-0b0c0d0e0f11'

class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_persist_uuid_and_preserve_legacy_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json'
            original=DeviceConfig(MAC,KEY,device_id=UUID)
            original.save(path)
            self.assertEqual(DeviceConfig.load(path),original)
            legacy=DeviceConfig(MAC,KEY)
            legacy.save(path)
            self.assertIsNone(DeviceConfig.load(path).device_id)
            with self.assertRaises(ValueError):DeviceConfig(MAC,KEY,device_id='not-a-uuid')

    async def test_authenticated_uuid_must_match_paired_uuid(self):
        client=Client(DeviceConfig(MAC,KEY,device_id=UUID))
        client.link.open=AsyncMock();client.link.close=AsyncMock()
        client.channel.handshake=AsyncMock(return_value={'device_id':UUID})
        async with client:
            self.assertEqual(client.device_id,UUID)
        for value in (OTHER,None,'bad'):
            client=Client(DeviceConfig(MAC,KEY,device_id=UUID))
            client.link.open=AsyncMock();client.link.close=AsyncMock()
            client.channel.handshake=AsyncMock(return_value={'device_id':value})
            with self.assertRaises(DeviceError):await client.__aenter__()
            client.link.close.assert_awaited_once()

    async def test_legacy_board_is_accepted_without_inventing_uuid(self):
        client=Client(DeviceConfig(MAC,KEY))
        client.link.open=AsyncMock();client.link.close=AsyncMock()
        client.channel.handshake=AsyncMock(return_value={})
        async with client:self.assertIsNone(client.device_id)
        client=Client(DeviceConfig(MAC,KEY))
        client.link.open=AsyncMock();client.link.close=AsyncMock()
        client.channel.handshake=AsyncMock(return_value={'device_id':UUID})
        async with client:self.assertEqual(client.device_id,UUID)
        client=Client(DeviceConfig(MAC,KEY))
        client.link.open=AsyncMock();client.link.close=AsyncMock()
        client.channel.handshake=AsyncMock(return_value={'device_id':None})
        with self.assertRaises(DeviceError):await client.__aenter__()
