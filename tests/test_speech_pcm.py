import asyncio
import json
import unittest
import zlib
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.channel import Channel, DeviceError

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
        self.assertEqual(send.await_args.kwargs, {})

    async def test_ticket_playback_failure_cleans_up_only_its_ticket(self):
        send = AsyncMock(side_effect=[{'ok': True}, DeviceError('broken'), {'ok': True}])
        client = self.client(send)
        client.channel.capabilities.update(capture_speech_ticket=1, capture_speech_cancel_ticket=1)

        with self.assertRaisesRegex(DeviceError, 'broken'):
            await client.play_speech_pcm(b'1234', capture_ticket=7)

        send.assert_awaited_with('speech_cancel', ticket=7)

    async def test_ticket_cancellation_drains_outstanding_ack(self):
        for waiting_for in ('speech_begin', 'speech_chunk', 'speech_commit', 'speech_status'):
            with self.subTest(waiting_for=waiting_for):
                ack_pending = asyncio.Event()
                release_ack = asyncio.Event()
                sent = []

                class Transport:
                    async def write(self, batch):
                        sent.append(json.loads(batch[3:]))

                    async def readline(self):
                        request = sent[-1]
                        kind = request['type']
                        if kind == waiting_for:
                            ack_pending.set()
                            await release_ack.wait()
                        reply = {'ok': True, 'boot': 1, 'epoch': request['epoch'], 'seq': request['seq']}
                        if kind == 'speech_status':
                            reply['speech_status'] = 'playing'
                        elif kind == 'capture_check':
                            reply['active'] = True
                        elif kind == 'speech_cancel' and waiting_for == 'speech_status':
                            reply.update(ok=False, error='unknown_capture')
                        return ('P1,' + json.dumps(reply) + '\n').encode()

                client = self.client(AsyncMock())
                client.channel = Channel(Transport(), 'aa:bb:cc:dd:ee:ff')
                client.channel.boot = 1
                client.channel.online = True
                client.channel.capabilities.update(speech_pcm=1, capture_speech_ticket=1,
                                                   capture_speech_cancel_ticket=1, capture_cancel_poll=1)
                playing = asyncio.create_task(client.play_speech_pcm(bytes(768), capture_ticket=7))
                try:
                    await asyncio.wait_for(ack_pending.wait(), 1)
                    playing.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(playing.done())
                    self.assertTrue(client._operation.locked())
                    # A second cancellation while draining must still leave the P1 exchange intact.
                    playing.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(client._operation.locked())
                    release_ack.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(playing, 1)

                    self.assertTrue(client.channel.online)
                    self.assertTrue(await client.capture_active(8))
                    expected = ['speech_begin', 'speech_chunk', 'speech_chunk', 'speech_commit', 'speech_status']
                    if waiting_for == 'speech_chunk':
                        expected = expected[:2]
                    else:
                        expected = expected[:expected.index(waiting_for) + 1]
                    self.assertEqual([request['type'] for request in sent], expected + ['speech_cancel', 'capture_check'])
                    self.assertEqual(sent[-2]['ticket'], 7)
                    self.assertEqual(sent[-1]['ticket'], 8)
                finally:
                    release_ack.set()
                    await asyncio.gather(playing, return_exceptions=True)

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
