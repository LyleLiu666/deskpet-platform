import asyncio
import unittest
import zlib
from unittest.mock import AsyncMock
from deskpet_platform.client import Client
from deskpet_platform.channel import DeviceError

class CapturePullTests(unittest.IsolatedAsyncioTestCase):
    def client(self, send, supported=True):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = type('Channel', (), {'capabilities': {'capture_pull': int(supported)}, 'send': send})()
        return client

    async def test_receive_checks_crc_then_clears_device(self):
        pcm = bytes(range(256)) * 4
        async def reply(kind, **fields):
            if kind == 'capture_chunk':
                offset = fields['offset']
                return {'data': pcm[offset:offset + 384].hex()}
            return {}
        send = AsyncMock(side_effect=reply)
        result = await self.client(send).receive_capture({'ticket': 7, 'bytes': len(pcm), 'crc32': zlib.crc32(pcm)})
        self.assertEqual(result, pcm)
        self.assertEqual([call.kwargs['offset'] for call in send.await_args_list if call.args[0] == 'capture_chunk'], [0, 384, 768])
        self.assertEqual(send.await_args.args[0], 'capture_finish')

    async def test_corrupt_chunk_and_crc_never_return_audio(self):
        for chunk, crc in (('00', 0), ('xyz', 0), ('0000', 1), ('00 00', 0)):
            with self.subTest(chunk=chunk):
                async def reply(kind, **_fields):
                    return {'data': chunk} if kind == 'capture_chunk' else {}
                send = AsyncMock(side_effect=reply)
                with self.assertRaises(DeviceError):
                    await self.client(send).receive_capture({'ticket': 1, 'bytes': 2, 'crc32': crc})
                self.assertEqual(send.await_args.args[0], 'capture_finish')

    async def test_cancellation_clears_device_recording(self):
        entered = asyncio.Event()
        async def reply(kind, **_fields):
            if kind == 'capture_chunk':
                entered.set()
                await asyncio.Future()
            return {}
        send = AsyncMock(side_effect=reply)
        task = asyncio.create_task(self.client(send).receive_capture({'ticket': 1, 'bytes': 2, 'crc32': 0}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(send.await_args.args[0], 'capture_finish')

    async def test_invalid_metadata_and_unsupported_device_never_fetch(self):
        send = AsyncMock()
        for metadata in ({}, {'ticket': True, 'bytes': 2, 'crc32': 0},
                         {'ticket': 1, 'bytes': 1, 'crc32': 0},
                         {'ticket': 1, 'bytes': 5760002, 'crc32': 0}):
            with self.assertRaises(DeviceError):
                await self.client(send).receive_capture(metadata)
        with self.assertRaises(DeviceError):
            await self.client(send, False).receive_capture({'ticket': 1, 'bytes': 2, 'crc32': 0})
        send.assert_not_awaited()
