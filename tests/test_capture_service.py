import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from deskpet_platform.mcp_server import BoardSession

class CaptureServiceTests(unittest.IsolatedAsyncioTestCase):
    def session(self, path, enabled=True):
        with patch.dict('os.environ', {'DESKPET_ENABLE_ONLINE_TRANSCRIPTION': '1' if enabled else '0',
                                       'DESKPET_ASR_API_KEY': 'test-key', 'DESKPET_ASR_MODEL': 'model/name'}):
            return BoardSession(path, no_token_probe=True)

    def board(self):
        client = type('Board', (), {})()
        client.channel = type('Channel', (), {'capabilities': {'capture_pull': 1, 'transcript_display': 1}})()
        client.poll_capture = AsyncMock(return_value={'ticket': 7, 'bytes': 2, 'crc32': 0})
        client.receive_capture = AsyncMock(return_value=b'\0\0')
        client.capture_result = AsyncMock()
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
            transcribe.assert_awaited_once_with(client, b'\0\0', api_key='test-key', model='model/name')
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
