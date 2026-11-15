import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.channel import DeviceError

class DashboardConfig(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client=Client.__new__(Client);client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{})();client.channel.capabilities={'dashboard_config':1,'quota_sources':1}
        client.channel.send=AsyncMock(return_value={'metric':4,'quota_source':2})
        return client
    async def test_metric_and_validation(self):
        client=self.client()
        self.assertEqual((await client.dashboard_config('weather'))['metric'],4)
        client.channel.send.assert_awaited_once_with('dashboard_config',mode='set',metric=4)
        client.channel.send.reset_mock()
        with self.assertRaises(ValueError):await client.dashboard_config('nonsense')
        client.channel.send.assert_not_awaited()
        client.channel.capabilities={}
        with self.assertRaises(DeviceError):await client.dashboard_config()
    async def test_session_status_and_source(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True);client=self.client();session.client=client
            self.assertEqual(await session.dashboard_config(None,None),{'status':'ok','metric':'weather','quota_source':'antigravity_gemini'})
            client.channel.send.reset_mock()
            client.channel.send.side_effect=[{'quota_source':1},{'metric':4,'quota_source':1}]
            self.assertEqual(await session.dashboard_config(None,'claude_code'),{'status':'ok','metric':'weather','quota_source':'claude_code'})
            self.assertEqual(client.channel.send.await_args_list[0].args,('quota_source',))
            with self.assertRaises(ValueError):await session.dashboard_config('speed','codex')

    async def test_rotation_selection_and_older_device_compatibility(self):
        client=self.client();client.channel.capabilities['dashboard_rotation']=1
        client.channel.send.return_value={'metric':3,'quota_source':0,'rotation':10}
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True);session.client=client
            value=await session.dashboard_config(None,None,['quota','weather'])
            self.assertEqual(value['rotation'],['quota','weather'])
            client.channel.send.assert_awaited_with('dashboard_config',mode='set',rotation=10)
            for invalid in ([],['speed','speed'],['auto'],['wrong']):
                with self.assertRaises(ValueError):await session.dashboard_config(None,None,invalid)
            with self.assertRaises(ValueError):await session.dashboard_config('auto',None,['speed'])
            client.channel.send.return_value={'metric':3,'quota_source':0,'rotation':0}
            with self.assertRaises(DeviceError):await session.dashboard_config(None,None)
            client.channel.capabilities.pop('dashboard_rotation')
            client.channel.send.return_value={'metric':3,'quota_source':0}
            self.assertEqual((await session.dashboard_config(None,None))['metric'],'auto')
            self.assertEqual(await session.dashboard_config(None,None,['speed']),{'status':'unsupported'})
