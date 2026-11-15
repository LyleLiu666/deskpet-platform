import unittest
import asyncio
from unittest.mock import AsyncMock,patch
from pathlib import Path
from tempfile import TemporaryDirectory
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.speech import validate_text, split_speech, SpeechTooLong

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
