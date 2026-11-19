import asyncio
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.client import Client, DeviceError
from deskpet_platform.pairing import DeviceConfig

class CaptureServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_check_rejects_malformed_or_unsupported_reply(self):
        client=Client.__new__(Client);client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{})();client.channel.capabilities={'capture_cancel_poll':1}
        client.channel.send=AsyncMock(return_value={'active':True})
        self.assertTrue(await client.capture_active(7))
        client.channel.send.assert_awaited_once_with('capture_check',ticket=7)
        client.channel.send.return_value={'active':1}
        with self.assertRaises(DeviceError):await client.capture_active(7)
        client.channel.capabilities={}
        with self.assertRaises(DeviceError):await client.capture_active(7)

    async def test_no_speech_requires_advertised_capability(self):
        client=Client.__new__(Client);client._operation=asyncio.Lock()
        client.channel=type('Channel',(),{})();client.channel.capabilities={'capture_mode':1}
        client.channel.send=AsyncMock(return_value={})
        with self.assertRaises(ValueError):await client.capture_result(7,False,failure='no_speech')
        client.channel.capabilities['capture_no_speech']=1
        await client.capture_result(7,False,failure='no_speech')
        client.channel.send.assert_awaited_once_with('capture_result',ticket=7,success=False,failure='no_speech')
        with self.assertRaises(ValueError):await client.capture_result(7,True,failure='no_speech')

    def session(self, path, enabled=True):
        with patch.dict('os.environ', {'DESKPET_ENABLE_ONLINE_TRANSCRIPTION': '1' if enabled else '0',
                                       'DESKPET_ASR_API_KEY': 'test-key', 'DESKPET_ASR_MODEL': 'model/name'}):
            return BoardSession(path, no_token_probe=True)

    def board(self):
        client = type('Board', (), {})()
        client.channel = type('Channel', (), {'capabilities': {'capture_pull': 1, 'transcript_display': 1, 'capture_transcript_ticket': 1, 'capture_cancel_poll': 1}})()
        client.poll_capture = AsyncMock(return_value={'ticket': 7, 'bytes': 2, 'crc32': 0})
        client.receive_capture = AsyncMock(return_value=b'\0\0')
        client.capture_result = AsyncMock()
        client.capture_active = AsyncMock(return_value=True)
        client.discard_capture = AsyncMock()
        client.capture_progress = AsyncMock()
        client.queue_transcript = AsyncMock()
        return client

    async def test_disabled_never_polls_or_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json', False)
            client = self.board(); session.client = client
            await session.refresh_capture(client)
            client.poll_capture.assert_not_awaited()

    async def test_explicit_capture_is_transcribed_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board(); session.client = client
            with patch('deskpet_platform.mcp_server.transcribe_to_device', new_callable=AsyncMock) as transcribe:
                await session.refresh_capture(client)
                await session.capture_task
            client.receive_capture.assert_awaited_once()
            transcribe.assert_awaited_once_with(client, b'\0\0', api_key='test-key', model='model/name', capture_ticket=7)
            client.capture_result.assert_awaited_once_with(7, True)

    async def test_local_capture_reports_result_and_rejects_empty_speech(self):
        from deskpet_platform.transcription import LocalTranscription, TranscriptionError
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            session.capture_service=LocalTranscription('/cli','/model','/vad')
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,return_value='你好') as transcribe:
                await session.refresh_capture(client);await session.capture_task
            transcribe.assert_awaited_once_with(b'\0\0',session.capture_service)
            client.queue_transcript.assert_awaited_once_with('你好',capture_ticket=7)
            client.capture_result.assert_awaited_once_with(7,True)
            client.queue_transcript.reset_mock();client.capture_result.reset_mock()
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,side_effect=TranscriptionError('no_speech')):
                await session.refresh_capture(client);await session.capture_task
            client.queue_transcript.assert_not_awaited()
            client.capture_result.assert_awaited_once_with(7,False)

    async def test_local_no_speech_uses_device_capability_and_falls_back(self):
        from deskpet_platform.transcription import LocalTranscription, TranscriptionError
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            session.capture_service=LocalTranscription('/cli','/model','/vad')
            client.channel.capabilities['capture_no_speech']=1
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,side_effect=TranscriptionError('no_speech')):
                await session.refresh_capture(client);await session.capture_task
            client.capture_result.assert_awaited_once_with(7,False,failure='no_speech')
            client.capture_result.reset_mock();client.channel.capabilities.pop('capture_no_speech')
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,side_effect=TranscriptionError('no_speech')):
                await session.refresh_capture(client);await session.capture_task
            client.capture_result.assert_awaited_once_with(7,False)

    async def test_local_voice_no_speech_never_reaches_model(self):
        from deskpet_platform.transcription import LocalTranscription, TranscriptionError
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            session.capture_service=LocalTranscription('/cli','/model','/vad')
            client.channel.capabilities.update(capture_mode=1,capture_no_speech=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            session.voice_backend=type('Backend',(),{})();session.voice_backend.complete=AsyncMock()
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,side_effect=TranscriptionError('no_speech')):
                await session.refresh_capture(client);await session.capture_task
            session.voice_backend.complete.assert_not_awaited()
            client.queue_transcript.assert_not_awaited()
            client.capture_result.assert_awaited_once_with(7,False,failure='no_speech')

    async def test_local_voice_capture_can_enter_conversation(self):
        from deskpet_platform.transcription import LocalTranscription
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            session.capture_service=LocalTranscription('/cli','/model','/vad')
            client.channel.capabilities.update(capture_mode=1,role_actions=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            backend=type('Backend',(),{})();backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好，我在。'})
            session.voice_backend=backend;session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished','duration_ms':1})
            with patch('deskpet_platform.mcp_server.transcribe_local_pcm',new_callable=AsyncMock,return_value='你好'):
                await session.refresh_capture(client);await session.capture_task
            backend.complete.assert_awaited_once()
            client.queue_transcript.assert_awaited_once_with('你好',capture_ticket=7)
            client.capture_result.assert_awaited_once_with(7,True)

    async def test_dashscope_capture_displays_the_final_transcript(self):
        from deskpet_platform.transcription import DashScopeTranscription
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board()
            session.client = client
            session.capture_service = DashScopeTranscription('test-key')
            with patch('deskpet_platform.mcp_server.transcribe_dashscope_pcm',
                       new_callable=AsyncMock, return_value='你好搭搭') as transcribe:
                await session.refresh_capture(client)
                await session.capture_task
            transcribe.assert_awaited_once()
            client.queue_transcript.assert_awaited_once_with('你好搭搭', capture_ticket=7)
            client.capture_result.assert_awaited_once_with(7, True)

    async def test_active_capture_does_not_poll_or_start_a_second_task(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board(); session.client = client
            session.capture_task = asyncio.create_task(asyncio.sleep(60))
            try:
                await session.refresh_capture(client)
                client.poll_capture.assert_not_awaited()
            finally:
                session.capture_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await session.capture_task

    async def test_service_failure_reports_failure_without_leaking_error(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board(); session.client = client
            with patch('deskpet_platform.mcp_server.transcribe_to_device', new_callable=AsyncMock, side_effect=RuntimeError('test-key')):
                await session.refresh_capture(client)
                await session.capture_task
            client.capture_result.assert_awaited_once_with(7, False)
            self.assertNotIn('test-key', str(session.snapshot))

    async def test_lost_connection_does_not_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board(); session.client = client
            async def receive(_metadata):
                session.client = None
                return b'\0\0'
            client.receive_capture.side_effect = receive
            with patch('deskpet_platform.mcp_server.transcribe_to_device', new_callable=AsyncMock) as transcribe:
                await session.refresh_capture(client)
                await session.capture_task
            transcribe.assert_not_awaited()

    async def test_cancelled_capture_cleanup_keeps_reconnected_voice_context(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            old, new = self.board(), self.board()
            session.client = old
            old.close = AsyncMock()
            for client in (old, new):
                client.channel.capabilities.update(capture_mode=1, voice_names=1, speech_pcm=1)
                client.poll_capture.return_value = {'ticket': 7, 'bytes': 2, 'crc32': 0, 'mode': 'voice'}
            backend = type('Backend', (), {})()
            backend.complete = AsyncMock(return_value={'role': 'assistant', 'content': '收到'})
            session.voice_backend = backend
            session.voice_names = AsyncMock(return_value={'status': 'ok', 'user_name': '', 'device_name': '搭搭'})
            session.speak_voice_reply = AsyncMock(return_value={'status': 'finished', 'duration_ms': 1})
            old_entered, old_cancelled, finish_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls = 0

            async def recognize(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    old_entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        old_cancelled.set()
                        await finish_old.wait()
                return '重连后的新问题'

            with patch('deskpet_platform.mcp_server.transcribe_pcm', side_effect=recognize):
                await session.refresh_capture(old)
                old_capture = session.capture_task
                await asyncio.wait_for(old_entered.wait(), 1)
                await session.drop()
                try:
                    await asyncio.wait_for(old_cancelled.wait(), 1)
                    session.client = new
                    await session.refresh_capture(new)
                    await asyncio.wait_for(session.capture_task, 1)
                    conversation = session.voice_conversation
                    self.assertIsNotNone(conversation)
                finally:
                    finish_old.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(old_capture, 1)
            self.assertIs(session.voice_conversation, conversation)
            old.close.assert_awaited_once()
            old.capture_result.assert_not_awaited()
            new.capture_result.assert_awaited_once_with(7, True)

    async def test_capture_poll_from_old_connection_cannot_replace_new_task(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            old, new = self.board(), self.board()
            session.client = old
            old.close = AsyncMock()
            old_polled, release_poll, release_capture = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def poll():
                old_polled.set()
                await release_poll.wait()
                return {'ticket': 7}

            async def capture(client, metadata):
                await release_capture.wait()

            old.poll_capture.side_effect = poll
            session._screen_capture = AsyncMock(side_effect=capture)
            pending = asyncio.create_task(session.refresh_capture(old))
            await asyncio.wait_for(old_polled.wait(), 1)
            await session.drop()
            session.client = new
            await session.refresh_capture(new)
            new_task = session.capture_task
            release_poll.set()
            try:
                await asyncio.wait_for(pending, 1)
                self.assertIs(session.capture_task, new_task)
            finally:
                release_capture.set()
                await asyncio.gather(new_task, session.capture_task)

    async def test_waiting_voice_reply_does_not_play_on_reconnected_device(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            old, new = self.board(), self.board()
            for client in (old, new):
                client.channel.capabilities['speech_pcm'] = 1
                client.play_speech_pcm = AsyncMock(return_value={'status': 'finished', 'duration_ms': 1})
            session.client = session.voice_active_client = old
            session.voice_active_ticket = 7
            old.close = AsyncMock()
            with patch('deskpet_platform.mcp_server.synthesize_speech', new_callable=AsyncMock, return_value=b'12'):
                async with session.speech_lock:
                    reply = asyncio.create_task(session.speak_voice_reply('上一轮的回复'))
                    await asyncio.sleep(0)
                    await session.drop()
                    session.client = new
                result = await asyncio.wait_for(reply, 1)
            self.assertEqual(result['status'], 'not_connected')
            new.play_speech_pcm.assert_not_awaited()

    async def test_old_firmware_does_not_start_uncancellable_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json')
            client=self.board();session.client=client
            client.channel.capabilities.pop('capture_transcript_ticket')
            await session.refresh_capture(client)
            client.poll_capture.assert_not_awaited()

    async def test_exiting_cancels_inflight_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.capture_active.return_value=False
            entered=asyncio.Event();cancelled=asyncio.Event()
            async def recognition(*_args,**_kwargs):
                entered.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            with patch('deskpet_platform.mcp_server.transcribe_to_device',side_effect=recognition):
                await session.refresh_capture(client)
                await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(session.capture_task,2)
            self.assertTrue(cancelled.is_set())
            client.capture_result.assert_awaited_once_with(7,False)

    async def test_voice_capture_transcribes_then_replies_with_context(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,role_actions=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            backend=type('Backend',(),{})()
            backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好，我在。'})
            session.voice_backend=backend
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished','duration_ms':1})
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='你好'):
                await session.refresh_capture(client);await session.capture_task
            backend.complete.assert_awaited_once()
            self.assertEqual(backend.complete.await_args.args[1],[]) # no unapproved tools
            session.speak_voice_reply.assert_awaited_once_with('你好，我在。')
            client.capture_result.assert_awaited_once_with(7,True)
            client.poll_capture.assert_awaited_once_with(voice_ready=True)

    async def test_voice_mode_without_model_discards_audio_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.channel.capabilities['capture_mode']=1
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            session.voice_backend=None
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock) as transcribe:
                await session.refresh_capture(client);await session.capture_task
            client.receive_capture.assert_not_awaited();transcribe.assert_not_awaited()
            client.discard_capture.assert_awaited_once_with(7)
            client.capture_result.assert_awaited_once_with(7,False,failure='model')

    async def test_unadvertised_voice_mode_never_runs_model(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            session.voice_backend=AsyncMock()
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock) as transcribe:
                await session.refresh_capture(client);await session.capture_task
            client.receive_capture.assert_not_awaited();transcribe.assert_not_awaited()
            client.discard_capture.assert_awaited_once_with(7)

    async def test_exit_during_voice_model_cancels_and_clears_context(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,voice_names=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            client.capture_active.return_value=False
            entered=asyncio.Event();cancelled=asyncio.Event()
            async def complete(*_args):
                entered.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            backend=type('Backend',(),{})();backend.complete=AsyncMock(side_effect=complete);session.voice_backend=backend
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock()
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='你好'):
                await session.refresh_capture(client);await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(session.capture_task,2)
            self.assertTrue(cancelled.is_set());self.assertIsNone(session.voice_conversation)
            session.speak_voice_reply.assert_not_awaited();client.capture_result.assert_awaited_once_with(7,False,failure='model')

    async def test_exit_during_voice_playback_cancels_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,voice_names=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            client.capture_active.return_value=False
            backend=type('Backend',(),{})();backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好'})
            session.voice_backend=backend
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            entered=asyncio.Event();cancelled=asyncio.Event()
            async def speak(_text):
                entered.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
            session.speak_voice_reply=AsyncMock(side_effect=speak)
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='你好'):
                await session.refresh_capture(client);await asyncio.wait_for(entered.wait(),1)
                await asyncio.wait_for(session.capture_task,2)
            self.assertTrue(cancelled.is_set());self.assertIsNone(session.voice_conversation)
            client.capture_result.assert_awaited_once_with(7,False,failure='playback')

    async def test_two_voice_turns_retain_context(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json');client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,voice_names=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            histories=[]
            async def complete(messages,_tools):
                histories.append([dict(message) for message in messages]);return {'role':'assistant','content':'收到'}
            backend=type('Backend',(),{})();backend.complete=AsyncMock(side_effect=complete);session.voice_backend=backend
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,side_effect=['第一句','第二句']):
                await session.refresh_capture(client);await session.capture_task
                await session.refresh_capture(client);await session.capture_task
            self.assertEqual([m['content'] for m in histories[1][1:]],['第一句','收到','第二句'])
            self.assertEqual(session.voice_names.await_count,1)

    async def test_leaving_voice_page_ends_context_even_if_the_idle_poll_is_missed(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.session(Path(directory) / 'device.json')
            client = self.board()
            session.client = client
            client.channel.capabilities.update(capture_mode=1, capture_voice_session=1, voice_names=1, speech_pcm=1)
            client.poll_capture.side_effect = [
                {'ticket': 7, 'mode': 'voice', 'voice_session': 7},
                {'ticket': 8, 'mode': 'voice', 'voice_session': 7},
                {'ticket': 9, 'mode': 'voice', 'voice_session': 9},
                {'voice_session': 0},
            ]
            histories = []

            async def complete(messages, _tools):
                histories.append([message['content'] for message in messages[1:]])
                return {'role': 'assistant', 'content': '收到'}

            backend = type('Backend', (), {})()
            backend.complete = AsyncMock(side_effect=complete)
            session.voice_backend = backend
            session.voice_names = AsyncMock(return_value={'status': 'ok', 'user_name': '', 'device_name': '搭搭'})
            session.speak_voice_reply = AsyncMock(return_value={'status': 'finished'})
            with patch('deskpet_platform.mcp_server.transcribe_pcm', new_callable=AsyncMock,
                       side_effect=['第一句', '追问', '重新进来']):
                for _ in range(3):
                    await session.refresh_capture(client)
                    await session.capture_task
                self.assertIsNotNone(session.voice_conversation)
                await session.refresh_capture(client)
            self.assertEqual(histories, [['第一句'], ['第一句', '收到', '追问'], ['重新进来']])
            self.assertIsNone(session.voice_conversation)
            self.assertEqual(session.voice_names.await_count, 2)

    async def test_device_cancel_stops_actual_pcm_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            session=self.session(Path(directory)/'device.json')
            client=Client.__new__(Client);client._operation=asyncio.Lock();client._capture=asyncio.Lock();client._speech=asyncio.Lock()
            playing=asyncio.Event();commands=[];speech_fields=[]
            async def send(kind,**fields):
                commands.append(kind)
                if kind=='speech_begin':speech_fields.append(fields)
                if kind=='capture_poll':return {'ticket':7,'bytes':2,'crc32':zlib.crc32(b'12'),'mode':'voice'}
                if kind=='capture_chunk':return {'data':b'12'.hex()}
                if kind=='speech_status':playing.set();return {'speech_status':'playing'}
                if kind=='capture_check':return {'active':not playing.is_set()}
                return {'ok':True}
            client.channel=type('Channel',(),{'capabilities':{'capture_pull':1,'capture_mode':1,'transcript_display':1,
                'capture_transcript_ticket':1,'capture_cancel_poll':1,'voice_names':1,'speech_pcm':1,
                'capture_speech_ticket':1},'send':AsyncMock(side_effect=send)})()
            session.client=client;backend=type('Backend',(),{})()
            backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好'})
            session.voice_backend=backend
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='你好'), \
                 patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock,return_value=b'12'):
                await session.refresh_capture(client);await asyncio.wait_for(playing.wait(),1)
                await asyncio.wait_for(session.capture_task,3)
            self.assertIn('capture_check',commands);self.assertIn('speech_cancel',commands)
            self.assertEqual(speech_fields,[{'bytes':2,'crc32':zlib.crc32(b'12'),'capture_ticket':7}])
            self.assertLess(commands.index('capture_check'),commands.index('speech_cancel'))
            self.assertIsNone(session.voice_conversation)

    async def test_authorized_tool_is_used_and_revocation_is_checked_before_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path);client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,role_actions=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            await session.voice_tool_settings(['list_role_actions'])
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.list_actions=AsyncMock(return_value={'status':'ok','actions':['touched']})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            entered=asyncio.Event();resume=asyncio.Event();rounds=0
            async def complete(_messages,tools):
                nonlocal rounds
                rounds+=1
                if rounds==1:
                    self.assertEqual([tool.name for tool in tools],['list_role_actions'])
                    entered.set();await resume.wait()
                    return {'role':'assistant','content':None,'tool_calls':[{'id':'one','type':'function',
                        'function':{'name':'list_role_actions','arguments':'{}'}}]}
                return {'role':'assistant','content':'已经做完。'}
            session.voice_backend=type('Backend',(),{'complete':AsyncMock(side_effect=complete)})()
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='做个动作'):
                await session.refresh_capture(client);await asyncio.wait_for(entered.wait(),1)
                await session.voice_tool_settings([]);resume.set();await asyncio.wait_for(session.capture_task,2)
            session.list_actions.assert_not_awaited()
            session.speak_voice_reply.assert_awaited_once_with('这项语音操作未获授权。')

    async def test_voice_tool_settings_show_descriptions_and_are_per_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path)
            initial=await session.voice_tool_settings()
            self.assertEqual(initial['enabled'],[])
            self.assertEqual({item['name'] for item in initial['available']},{'device_status','list_role_actions','play_role_action'})
            updated=await session.voice_tool_settings(['device_status'])
            self.assertEqual(updated['enabled'],['device_status'])
            self.assertEqual((await self.session(path).voice_tool_settings())['enabled'],['device_status'])

    async def test_voice_agent_can_execute_only_authorized_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path);client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,role_actions=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            await session.voice_tool_settings(['list_role_actions'])
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.list_actions=AsyncMock(return_value={'status':'ok','actions':['touched']})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            backend=type('Backend',(),{})();backend.complete=AsyncMock(side_effect=[
                {'role':'assistant','content':None,'tool_calls':[{'id':'one','type':'function',
                  'function':{'name':'list_role_actions','arguments':'{}'}}]},
                {'role':'assistant','content':'可以做触摸互动。'}])
            session.voice_backend=backend
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='做个动作'):
                await session.refresh_capture(client);await session.capture_task
            session.list_actions.assert_awaited_once_with()
            session.speak_voice_reply.assert_awaited_once_with('可以做触摸互动。')
            client.capture_result.assert_awaited_once_with(7,True)

    async def test_permission_change_rebuilds_tool_registry_before_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path);client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            await session.voice_tool_settings(['device_status'])
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            calls=[]
            async def complete(messages,tools):
                calls.append(([m.get('content') for m in messages],[t.name for t in tools]))
                return {'role':'assistant','content':'收到'}
            backend=type('Backend',(),{})();backend.complete=AsyncMock(side_effect=complete);session.voice_backend=backend
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,side_effect=['第一句','第二句']):
                await session.refresh_capture(client);await session.capture_task
                await session.voice_tool_settings([])
                await session.refresh_capture(client);await session.capture_task
            self.assertEqual(calls[0][1],['device_status']);self.assertEqual(calls[1][1],[])
            self.assertEqual(calls[1][0][1:],['第二句'])

    async def test_authorized_role_action_waits_for_view_and_is_bound_to_voice_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path);client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,capture_voice_role=1,role_actions=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            client.list_actions=AsyncMock(return_value=['idle','touched'])
            client.capture_role_view=AsyncMock();client.capture_role_play=AsyncMock(return_value={'action_status':'started'})
            await session.voice_tool_settings(['play_role_action'])
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            backend=type('Backend',(),{})();backend.complete=AsyncMock(side_effect=[
                {'role':'assistant','content':None,'tool_calls':[{'id':'one','type':'function',
                  'function':{'name':'play_role_action','arguments':'{"action":"touched"}'}}]},
                {'role':'assistant','content':'已经做完。'}]);session.voice_backend=backend
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='做个动作'):
                await session.refresh_capture(client);await session.capture_task
            client.capture_role_view.assert_awaited_once_with(7)
            client.capture_role_play.assert_awaited_once_with(7,1,0)
            session.speak_voice_reply.assert_awaited_once_with('操作已开始，尚未确认完成。')

    async def test_voice_role_not_exposed_to_older_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            session=self.session(path);client=self.board();session.client=client
            client.channel.capabilities.update(capture_mode=1,voice_names=1,speech_pcm=1)
            client.poll_capture.return_value={'ticket':7,'bytes':2,'crc32':0,'mode':'voice'}
            await session.voice_tool_settings(['play_role_action'])
            session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'','device_name':'搭搭'})
            session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
            backend=type('Backend',(),{})();backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好'})
            session.voice_backend=backend
            with patch('deskpet_platform.mcp_server.transcribe_pcm',new_callable=AsyncMock,return_value='你好'):
                await session.refresh_capture(client);await session.capture_task
            self.assertEqual(backend.complete.await_args.args[1],[])
