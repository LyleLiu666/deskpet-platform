import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from deskpet_platform.client import Client, DeviceError
from deskpet_platform.mcp_server import BoardSession


class VoiceNamesTest(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client=Client.__new__(Client);client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{})();client.channel.capabilities={'voice_names':1}
        client.channel.send=AsyncMock(return_value={'user_name':'小刘','device_name':'托肯'})
        return client

    async def test_read_and_partial_write(self):
        client=self.client()
        await client.voice_names()
        client.channel.send.assert_awaited_with('voice_names',mode='status')
        await client.voice_names(user_name='小刘')
        client.channel.send.assert_awaited_with('voice_names',mode='set',user_name='小刘')
        await client.voice_names(user_name='',device_name='小搭')
        client.channel.send.assert_awaited_with('voice_names',mode='set',user_name='',device_name='小搭')

    async def test_input_validation_and_old_firmware(self):
        client=self.client()
        for args in ({'device_name':''},{'user_name':' x'},{'user_name':'a\x00b'},
                     {'device_name':'a\n'}, {'user_name':'字'*22}, {'user_name':'\ud800'}):
            with self.subTest(args=args),self.assertRaises(ValueError):await client.voice_names(**args)
        client.channel.send.assert_not_awaited()
        client.channel.capabilities={}
        with self.assertRaises(DeviceError):await client.voice_names()

    async def test_session_validates_reply_and_reports_old_device(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            self.assertEqual((await session.voice_names())['status'],'not_connected')
            client=self.client();session.client=client
            self.assertEqual(await session.voice_names(device_name='托肯'),
                             {'status':'ok','user_name':'小刘','device_name':'托肯'})
            client.channel.send.return_value={'user_name':'小刘','device_name':''}
            with self.assertRaises(DeviceError):await session.voice_names()
            client.channel.capabilities={}
            self.assertEqual((await session.voice_names())['status'],'unsupported')
