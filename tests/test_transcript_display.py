import asyncio
import unittest
import zlib
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.channel import DeviceError


class TranscriptDisplayTests(unittest.IsolatedAsyncioTestCase):
    def client(self, send, supported=True):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = type('Channel', (), {'capabilities': {'transcript_display': int(supported)}, 'send': send})()
        return client

    async def test_long_text_is_sent_in_order(self):
        send = AsyncMock(return_value={})
        text = '合成识别结果。' * 100
        await self.client(send).queue_transcript(text)
        data = text.encode()
        self.assertEqual(send.await_args_list[0].kwargs, {'bytes': len(data), 'crc32': zlib.crc32(data)})
        chunks = [call for call in send.await_args_list if call.args[0] == 'transcript_chunk']
        self.assertEqual(bytes.fromhex(''.join(call.kwargs['data'] for call in chunks)), data)
        self.assertEqual([call.kwargs['offset'] for call in chunks], list(range(0, len(data), 384)))
        self.assertEqual(send.await_args.args[0], 'transcript_commit')

    async def test_empty_result_and_invalid_text(self):
        send = AsyncMock(return_value={})
        client = self.client(send)
        for value in ('a' * 4097, 'x\0y', 'x\x7fy', '\ud800', 42):
            with self.assertRaises(ValueError):
                await client.queue_transcript(value)
        send.assert_not_awaited()
        await client.queue_transcript('')
        self.assertEqual([call.args[0] for call in send.await_args_list], ['transcript_begin', 'transcript_commit'])

    async def test_failure_cancels_and_old_devices_are_rejected(self):
        send = AsyncMock(side_effect=[{}, DeviceError('failed'), {}])
        with self.assertRaises(DeviceError):
            await self.client(send).queue_transcript('text')
        self.assertEqual(send.await_args.args[0], 'transcript_cancel')
        with self.assertRaises(DeviceError):
            await self.client(AsyncMock(), supported=False).queue_transcript('text')
