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
            async def read(identity):
                async with session.lock:return {'status':'finished'}
            session.read_notification=AsyncMock(side_effect=read)
            async with session.lock:await session.refresh_read_request(client)
            await asyncio.wait_for(session.screen_speech_task,1)
            session.read_notification.assert_awaited_once_with(42)
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
            async def read(_identity):session.client=None;return {'status':'finished'}
            session.read_notification=AsyncMock(side_effect=read)
            await session.refresh_read_request(client);await session.screen_speech_task
            client.read_request_finish.assert_not_awaited()
