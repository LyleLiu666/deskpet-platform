import unittest
import asyncio
import wave
from unittest.mock import AsyncMock,patch
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from aiohttp import ClientConnectionError
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.speech import validate_text, split_speech, synthesize_speech, SpeechServiceError, SpeechTooLong

class Speech(unittest.IsolatedAsyncioTestCase):
    def test_input_bounds(self):
        self.assertEqual(validate_text('  测试  '),'测试')
        for text in ('','   ','x'*81,'a\nb','a\n',42):
            with self.assertRaises(ValueError):validate_text(text)

    def test_split_preserves_order_and_bounds(self):
        text='abcdefghijklmno，第二句内容继续。'
        parts=split_speech(text)
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(0<len(part)<=12 for part in parts))
        self.assertEqual(split_speech('abcdefghijkl，后续'),['abcdefghijk','l，','后续'])

    async def test_synthesis_retries_a_transient_connection_failure(self):
        pcm = b'\x01\x00\x02\x00'
        first_save = AsyncMock(side_effect=ClientConnectionError())
        second_save = AsyncMock()

        async def convert(*args, **kwargs):
            with wave.open(args[-1], 'wb') as target:
                target.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                target.writeframes(pcm)
            return SimpleNamespace(wait=AsyncMock(), returncode=0)

        with patch('edge_tts.Communicate', side_effect=[
            SimpleNamespace(save=first_save),
            SimpleNamespace(save=second_save),
        ]), patch('deskpet_platform.speech.asyncio.create_subprocess_exec', side_effect=convert):
            self.assertEqual(await synthesize_speech('测试'), pcm)
        first_save.assert_awaited_once()
        second_save.assert_awaited_once()

    async def test_synthesis_reports_failure_after_two_attempts(self):
        save = AsyncMock(side_effect=ClientConnectionError('remote failure'))
        with patch('edge_tts.Communicate', return_value=SimpleNamespace(save=save)):
            with self.assertRaisesRegex(SpeechServiceError, '在线语音合成暂时不可用') as error:
                await synthesize_speech('测试')
        self.assertEqual(error.exception.code, 'service_unreachable')
        self.assertEqual(save.await_count, 2)

    async def test_no_network_when_board_unavailable(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock) as synth:
                result=await session.speak('测试')
            self.assertEqual(result['status'],'not_connected');synth.assert_not_awaited()
            session.client=type('Board',(),{'channel':type('Channel',(),{'capabilities':{}})()})()
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock) as synth:
                self.assertEqual((await session.speak('测试'))['status'],'unsupported')
            synth.assert_not_awaited()

    async def test_screen_read_without_cancel_support_fails_before_synthesis(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            session.client=type('Board',(),{'channel':type('Channel',(),{'capabilities':{'speech_pcm':1}})()})()
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock) as synth:
                result=await session.speak('测试',read_ticket=7)
            self.assertEqual(result['status'],'read_cancel_not_supported')
            synth.assert_not_awaited()

    async def test_explicit_request_synthesizes_and_plays(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            client.play_speech_pcm=AsyncMock(return_value={'status':'finished','duration_ms':1});session.client=client
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock,return_value=b'12') as synth:
                result=await session.speak(' 测试 ')
            synth.assert_awaited_once_with('测试');client.play_speech_pcm.assert_awaited_once_with(b'12')
            self.assertEqual(result['status'],'finished')

    async def test_notification_read_is_explicit_and_does_not_mark_seen(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            identity=session.notifications.add_agent_event('codex','session','turn','completed','任务完成')
            session.speak=AsyncMock(return_value={'status':'finished'})
            self.assertEqual((await session.read_notification(identity))['status'],'finished')
            session.speak.assert_awaited_once_with('任务完成')
            self.assertFalse(session.notifications.list()[0]['seen'])
            self.assertEqual((await session.read_notification(999))['status'],'not_found')

    async def test_playback_keeps_the_session_available_to_status_updates(self):
        with TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / 'device.json', no_token_probe=True)

            async def play(pcm):
                self.assertEqual(pcm, b'12')
                self.assertFalse(session.lock.locked())
                return {'status': 'finished', 'duration_ms': 1}

            session.client = SimpleNamespace(
                channel=SimpleNamespace(capabilities={'speech_pcm': 1}), play_speech_pcm=play)
            with patch('deskpet_platform.mcp_server.synthesize_speech', new_callable=AsyncMock,
                       return_value=b'12'):
                self.assertEqual((await session.speak('测试'))['status'], 'finished')

    async def test_connection_change_during_synthesis_prevents_dispatch(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            client.play_speech_pcm=AsyncMock();session.client=client
            async def synth(_text):session.client=None;return b'12'
            with patch('deskpet_platform.mcp_server.synthesize_speech',side_effect=synth):
                self.assertEqual((await session.speak('测试'))['status'],'not_connected')
            client.play_speech_pcm.assert_not_awaited()

    async def test_long_text_is_spoken_in_order_and_retries_oversized_audio(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            client.play_speech_pcm=AsyncMock(return_value={'status':'finished','duration_ms':1});session.client=client
            async def synth(text):
                if len(text)>6:raise SpeechTooLong('long')
                return (text*2).encode()
            with patch('deskpet_platform.mcp_server.synthesize_speech',side_effect=synth):
                result=await session.speak('abcdefghijklmnop')
            self.assertEqual(result['status'],'finished')
            self.assertEqual([c.args[0] for c in client.play_speech_pcm.await_args_list],
                             [b'abcdefabcdef',b'ghijklghijkl',b'mnopmnop'])

    async def test_concurrent_requests_do_not_interleave(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            spoken=[]
            async def play(data):spoken.append(data.decode());await asyncio.sleep(0);return {'status':'finished','duration_ms':1}
            client.play_speech_pcm=play;session.client=client
            async def synth(text):await asyncio.sleep(0);return text.encode()
            with patch('deskpet_platform.mcp_server.synthesize_speech',side_effect=synth):
                await asyncio.gather(session.speak('abcdefghijklm'),session.speak('第二条'))
            self.assertEqual(spoken,['abcdefghijkl','m','第二条'])

    async def test_voice_reply_reads_long_multiline_text_in_order(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            client.play_speech_pcm=AsyncMock(return_value={'status':'finished','duration_ms':1});session.client=client
            text='长回答'*50+'\n第二段。'
            async def synth(part):return part.encode()
            with patch('deskpet_platform.mcp_server.synthesize_speech',side_effect=synth):
                result=await session.speak_voice_reply(text)
            self.assertEqual(result['status'],'finished')
            self.assertEqual(b''.join(call.args[0] for call in client.play_speech_pcm.await_args_list).decode(),text.replace('\n',' '))

    async def test_voice_reply_cancellation_stops_further_playback(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            entered=asyncio.Event()
            async def play(_data):entered.set();await asyncio.Event().wait()
            client.play_speech_pcm=AsyncMock(side_effect=play);session.client=client
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock,return_value=b'12') as synth:
                task=asyncio.create_task(session.speak_voice_reply('回答'*50))
                await entered.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(synth.await_count,1)
                self.assertEqual(client.play_speech_pcm.await_count,1)

    async def test_voice_reply_rejects_invalid_or_excessive_text(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            for text in ('  \n  ', 'a\x00b', '字'*2731, '\ud800', None):
                with self.assertRaises(ValueError):await session.speak_voice_reply(text)

    async def test_voice_reply_stops_if_board_disconnects_between_parts(self):
        with TemporaryDirectory() as directory:
            session=BoardSession(Path(directory)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1}})()
            async def play(_data):
                session.client=None
                return {'status':'finished','duration_ms':1}
            client.play_speech_pcm=AsyncMock(side_effect=play);session.client=client
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock,return_value=b'12') as synth:
                result=await session.speak_voice_reply('回答'*50)
            self.assertEqual(result['status'],'not_connected')
            self.assertEqual(client.play_speech_pcm.await_count,1)
            self.assertEqual(synth.await_count,1)
