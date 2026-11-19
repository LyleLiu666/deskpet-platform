import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from deskpet_platform.mcp_server import BoardSession

class ScreenSpeech(unittest.IsolatedAsyncioTestCase):
    async def test_take_runs_outside_session_lock_and_reports_result(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=type('Board',(),{})()
            client.read_request_take=AsyncMock(return_value={'identity':42,'ticket':7})
            client.read_request_finish=AsyncMock();session.client=client
            async def read(identity, *, read_ticket):
                async with session.lock:return {'status':'finished'}
            session.read_notification=AsyncMock(side_effect=read)
            async with session.lock:await session.refresh_read_request(client)
            await asyncio.wait_for(session.screen_speech_task,1)
            session.read_notification.assert_awaited_once_with(42,read_ticket=7)
            client.read_request_finish.assert_awaited_once_with(7,True)

    async def test_missing_notification_or_synthesis_error_is_reported(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.read_request_take=AsyncMock(return_value={'identity':42,'ticket':7})
            client.read_request_finish=AsyncMock();session.client=client
            session.read_notification=AsyncMock(side_effect=RuntimeError('synthesis failed'))
            await session.refresh_read_request(client);await session.screen_speech_task
            client.read_request_finish.assert_awaited_once_with(7,False)

    async def test_empty_and_malformed_request_does_not_play(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.read_request_take=AsyncMock(side_effect=[{}, {'identity':True,'ticket':1}])
            session.client=client;session.read_notification=AsyncMock()
            await session.refresh_read_request(client);await session.refresh_read_request(client)
            session.read_notification.assert_not_awaited()

    async def test_connection_change_discards_old_receipt(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.read_request_take=AsyncMock(return_value={'identity':42,'ticket':7})
            client.read_request_finish=AsyncMock();session.client=client
            async def read(_identity, *, read_ticket):session.client=None;return {'status':'finished'}
            session.read_notification=AsyncMock(side_effect=read)
            await session.refresh_read_request(client);await session.screen_speech_task
            client.read_request_finish.assert_not_awaited()

    async def test_cancelled_ticket_stops_later_segments(self):
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=type('Board',(),{})();client.channel=type('Channel',(),{'capabilities':{'speech_pcm':1,'read_speech_ticket':1}})()
            client.play_speech_pcm=AsyncMock(side_effect=[{'status':'finished','duration_ms':1},RuntimeError('read_cancelled')])
            session.client=client
            from unittest.mock import patch
            with patch('deskpet_platform.mcp_server.synthesize_speech',new_callable=AsyncMock,return_value=b'12') as synth:
                with self.assertRaises(RuntimeError):await session.speak('abcdefghijklmnopqrstuvwxy',read_ticket=7)
            self.assertEqual(synth.await_count,2)
            self.assertEqual([call.kwargs for call in client.play_speech_pcm.await_args_list],[{'read_ticket':7},{'read_ticket':7}])

    async def test_device_rejects_segment_after_exit_during_synthesis(self):
        from deskpet_platform.client import Client
        from deskpet_platform.channel import CommandRejected
        from unittest.mock import patch
        with TemporaryDirectory() as folder:
            session=BoardSession(Path(folder)/'device.json',no_token_probe=True)
            client=Client.__new__(Client);client._operation=asyncio.Lock();client._speech=asyncio.Lock()
            class Board:
                capabilities={'speech_pcm':1,'read_speech_ticket':1}
                active=True
                starts=[]
                async def send(self,kind,**fields):
                    if kind=='speech_begin':
                        self.starts.append(fields['read_ticket'])
                        if not self.active:raise CommandRejected('read_cancelled')
                    return {'speech_status':'finished'} if kind=='speech_status' else {}
            board=Board();client.channel=board;session.client=client
            count=0
            async def synth(_part):
                nonlocal count
                count+=1
                if count==2:board.active=False # Device left the message during synthesis.
                return b'12'
            with patch('deskpet_platform.mcp_server.synthesize_speech',side_effect=synth):
                with self.assertRaises(CommandRejected) as error:
                    await session.speak('abcdefghijklmnopqrstuvwxy',read_ticket=7)
            self.assertEqual(error.exception.code,'read_cancelled')
            self.assertEqual(board.starts,[7,7]);self.assertEqual(count,2)
