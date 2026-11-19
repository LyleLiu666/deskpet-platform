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
        client._speech=asyncio.Lock()
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

    async def test_screen_read_sends_ticket_and_requires_device_support(self):
        async def reply(kind,**fields):
            return {'speech_status':'finished'} if kind=='speech_status' else {'ok':True}
        send=AsyncMock(side_effect=reply);client=self.client(send)
        with self.assertRaises(DeviceError):await client.play_speech_pcm(b'12',read_ticket=7,poll_interval=0)
        send.assert_not_awaited()
        client.channel.capabilities['read_speech_ticket']=1
        await client.play_speech_pcm(b'12',read_ticket=7,poll_interval=0)
        self.assertEqual(send.await_args_list[0].kwargs['read_ticket'],7)

    async def test_capture_can_be_checked_during_playback(self):
        playing=asyncio.Event();finish=asyncio.Event()
        async def reply(kind,**fields):
            if kind=='speech_status':
                playing.set();return {'speech_status':'finished' if finish.is_set() else 'playing'}
            if kind=='capture_check':return {'active':True}
            return {'ok':True}
        client=self.client(AsyncMock(side_effect=reply));client.channel.capabilities['capture_cancel_poll']=1
        task=asyncio.create_task(client.play_speech_pcm(b'12',poll_interval=.05))
        try:
            await asyncio.wait_for(playing.wait(),1)
            self.assertTrue(await asyncio.wait_for(client.capture_active(7),.3))
            finish.set();self.assertEqual((await asyncio.wait_for(task,1))['status'],'finished')
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task

    async def test_parallel_speech_uploads_remain_serialized(self):
        playing=asyncio.Event();finish=asyncio.Event();starts=[]
        async def reply(kind,**fields):
            if kind=='speech_begin':starts.append(fields['crc32'])
            if kind=='speech_status':
                playing.set();return {'speech_status':'finished' if finish.is_set() else 'playing'}
            return {'ok':True}
        client=self.client(AsyncMock(side_effect=reply))
        first=asyncio.create_task(client.play_speech_pcm(b'12',poll_interval=.01))
        await asyncio.wait_for(playing.wait(),1)
        second=asyncio.create_task(client.play_speech_pcm(b'34',poll_interval=.01))
        await asyncio.sleep(.05);self.assertEqual(starts,[zlib.crc32(b'12')])
        finish.set();await asyncio.wait_for(asyncio.gather(first,second),1)
        self.assertEqual(starts,[zlib.crc32(b'12'),zlib.crc32(b'34')])

    async def test_capture_check_can_run_between_audio_chunks(self):
        first_chunk=asyncio.Event();continue_upload=asyncio.Event()
        async def reply(kind,**fields):
            if kind=='speech_chunk' and fields['offset']==0:
                first_chunk.set();await continue_upload.wait()
            if kind=='capture_check':return {'active':True}
            return {'speech_status':'finished'} if kind=='speech_status' else {'ok':True}
        client=self.client(AsyncMock(side_effect=reply));client.channel.capabilities['capture_cancel_poll']=1
        task=asyncio.create_task(client.play_speech_pcm(bytes(768)))
        try:
            await asyncio.wait_for(first_chunk.wait(),1)
            check=asyncio.create_task(client.capture_active(7))
            await asyncio.sleep(0);continue_upload.set()
            self.assertTrue(await asyncio.wait_for(check,1));await asyncio.wait_for(task,1)
            names=[call.args[0] for call in client.channel.send.await_args_list]
            self.assertLess(names.index('capture_check'),names.index('speech_commit'))
        finally:
            continue_upload.set()
            if not task.done():task.cancel()
