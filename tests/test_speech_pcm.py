import asyncio
import unittest
import zlib
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.channel import DeviceError

class SpeechPCM(unittest.IsolatedAsyncioTestCase):
    def client(self,send):
        client=Client.__new__(Client)
        client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1},'send':send})()
        return client

    async def test_chunks_and_completion(self):
        states=iter(['queued','playing','finished'])
        async def reply(kind,**fields):
            return {'speech_status':next(states)} if kind=='speech_status' else {'ok':True}
        send=AsyncMock(side_effect=reply);client=self.client(send);audio=bytes(range(256))*3
        result=await client.play_speech_pcm(audio,poll_interval=0)
        self.assertEqual(result['status'],'finished')
        self.assertEqual(send.await_args_list[0].kwargs,{'bytes':len(audio),'crc32':zlib.crc32(audio)})
        chunks=[item for item in send.await_args_list if item.args[0]=='speech_chunk']
        self.assertEqual([item.kwargs['offset'] for item in chunks],[0,384])
        self.assertEqual(bytes.fromhex(''.join(item.kwargs['data'] for item in chunks)),audio)

    async def test_invalid_and_failed_upload_cancel(self):
        send=AsyncMock(side_effect=[{'ok':True},DeviceError('broken'),{'ok':True}]);client=self.client(send)
        for value in (b'',b'x',b'a'*160002,'text'):
            with self.assertRaises(ValueError):await client.play_speech_pcm(value)
        send.assert_not_awaited()
        with self.assertRaises(DeviceError):await client.play_speech_pcm(b'1234')
        self.assertEqual(send.await_args.args[0],'speech_cancel')

    async def test_device_interruption_is_not_reported_as_success(self):
        async def reply(kind,**fields):return {'speech_status':'interrupted'} if kind=='speech_status' else {'ok':True}
        with self.assertRaises(DeviceError):await self.client(AsyncMock(side_effect=reply)).play_speech_pcm(b'12',poll_interval=0)
