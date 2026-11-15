import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.channel import DeviceError


class AppearanceConfigTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client=Client.__new__(Client);client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{})();client.channel.capabilities={'appearance_config':1}
        client.channel.send=AsyncMock(return_value={'theme':'night','wallpaper':'/sdcard/Photos/p-123.jpg'})
        return client

    async def test_status_theme_and_wallpaper(self):
        client=self.client()
        await client.appearance_config()
        client.channel.send.assert_awaited_with('appearance_config',mode='status')
        await client.appearance_config(theme='night')
        client.channel.send.assert_awaited_with('appearance_config',mode='theme',value='night')
        await client.appearance_config(wallpaper='photos/p-123.jpg')
        client.channel.send.assert_awaited_with('appearance_config',mode='wallpaper',value='/sdcard/Photos/p-123.jpg')
        await client.appearance_config(wallpaper='')
        client.channel.send.assert_awaited_with('appearance_config',mode='wallpaper',value='')
        await client.appearance_config(wallpaper='p-1234567890abcdef')
        client.channel.send.assert_awaited_with('appearance_config',mode='wallpaper',value='/sdcard/Photos/p-1234567890abcdef.jpg')
        await client.appearance_config(theme='forest.json')
        client.channel.send.assert_awaited_with('appearance_config',mode='theme',value='forest.json')

    async def test_reject_unsafe_or_ambiguous_inputs(self):
        client=self.client()
        for kwargs in ({'theme':'night','wallpaper':''},{'theme':'../night.json'},
                       {'wallpaper':'photos/../secret.jpg'},{'wallpaper':'media/.hidden.jpg'},
                       {'wallpaper':'other/p.jpg'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):await client.appearance_config(**kwargs)
        client.channel.send.assert_not_awaited()

    async def test_session_returns_status(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True);session.client=self.client()
            result=await session.appearance_config('night',None)
            self.assertEqual(result,{'status':'ok','theme':'night','wallpaper':'photos/p-123.jpg'})
            session.client.channel.send.return_value={'theme':'night','wallpaper':'/sdcard/Photos/../secret.jpg'}
            with self.assertRaises(DeviceError):await session.appearance_config(None,None)


if __name__ == '__main__': unittest.main()
