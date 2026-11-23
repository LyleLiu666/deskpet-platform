import asyncio
import io
import gzip
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave
from unittest.mock import AsyncMock
from email.parser import BytesParser
from email.policy import default

import httpx

from deskpet_platform.channel import DeviceError
from deskpet_platform.transcription import MAX_PCM_BYTES, TranscriptionError, transcribe_pcm, transcribe_to_device, configured_service


class TranscriptionTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == 'posix', 'requires POSIX file permissions')
    def test_dashscope_loads_only_private_regular_key_file(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'key'
            path.write_bytes(b'fixture-key\n')
            path.chmod(0o600)
            base = {'DESKPET_ENABLE_DASHSCOPE_TRANSCRIPTION': '1', 'DASHSCOPE_API_KEY_FILE': str(path)}
            self.assertEqual(configured_service(base).api_key, 'fixture-key')
            self.assertEqual(configured_service({**base, 'DASHSCOPE_API_KEY': 'override-key'}).api_key,
                             'override-key')
            self.assertIsNone(configured_service({**base, 'DASHSCOPE_API_KEY': ''}))
            alias = Path(root) / 'alias'
            alias.symlink_to(path)
            self.assertIsNone(configured_service({**base, 'DASHSCOPE_API_KEY_FILE': str(alias)}))
            self.assertIsNone(configured_service({**base, 'DASHSCOPE_API_KEY_FILE': root}))
            path.chmod(0o644)
            self.assertIsNone(configured_service(base))
            path.chmod(0o600)
            for data in (b'abc\nxyz\n', b'a' * 129, b'\xff'):
                path.write_bytes(data)
                self.assertIsNone(configured_service(base))

    def test_online_recognition_requires_complete_explicit_configuration(self):
        self.assertIsNone(configured_service({}))
        base = {'DESKPET_ENABLE_ONLINE_TRANSCRIPTION': '1', 'DESKPET_ASR_API_KEY': 'test-key', 'DESKPET_ASR_MODEL': 'model/name'}
        self.assertEqual(configured_service(base), ('test-key', 'model/name'))
        for patch in ({'DESKPET_ENABLE_ONLINE_TRANSCRIPTION': '0'}, {'DESKPET_ASR_API_KEY': 'bad\nkey'}, {'DESKPET_ASR_MODEL': '../x'}):
            self.assertIsNone(configured_service({**base, **patch}))

    async def test_synthetic_pcm_uses_expected_multipart_without_local_files(self):
        pcm=b'\x01\x00\xfe\xff'*8
        async def handler(request):
            self.assertEqual(str(request.url),'https://api.siliconflow.cn/v1/audio/transcriptions')
            self.assertEqual(request.headers['authorization'],'Bearer test-key')
            self.assertNotIn('cookie',request.headers)
            body=await request.aread()
            mime=BytesParser(policy=default).parsebytes(
                b'Content-Type: '+request.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+body)
            parts={item.get_param('name',header='content-disposition'):item for item in mime.iter_parts()}
            self.assertEqual(parts['model'].get_payload(decode=True),b'FunAudioLLM/SenseVoiceSmall')
            with wave.open(io.BytesIO(parts['file'].get_payload(decode=True)),'rb') as source:
                self.assertEqual((source.getnchannels(),source.getsampwidth(),source.getframerate()),(1,2,24000))
                self.assertEqual(source.readframes(source.getnframes()),pcm)
            return httpx.Response(200,json={'text':'这是合成测试。'})
        result=await transcribe_pcm(pcm,api_key='test-key',model='FunAudioLLM/SenseVoiceSmall',transport=httpx.MockTransport(handler))
        self.assertEqual(result,'这是合成测试。')

    async def test_invalid_input_never_contacts_service(self):
        def forbidden(_request):raise AssertionError('network must not be called')
        transport=httpx.MockTransport(forbidden)
        for pcm in (b'',b'\x00',b'\x00'*(MAX_PCM_BYTES+2)):
            with self.subTest(length=len(pcm)),self.assertRaises(ValueError):
                await transcribe_pcm(pcm,api_key='test-key',model='model/name',transport=transport)
        for key in ('','bad\r\nheader',' '):
            with self.subTest(key=key),self.assertRaises(ValueError):
                await transcribe_pcm(b'\0\0',api_key=key,model='model/name',transport=transport)
        for model in ('','../x','model\nname'):
            with self.subTest(model=model),self.assertRaises(ValueError):
                await transcribe_pcm(b'\0\0',api_key='test-key',model=model,transport=transport)

    async def test_errors_do_not_echo_remote_content_or_key(self):
        for status,body in ((401,b'secret-key private audio'),(500,b'secret-key private audio'),
                            (302,b'secret-key private audio'),(200,b'not json'),
                            (200,json.dumps({'text':'字'*2000}).encode())):
            with self.subTest(status=status),self.assertRaises(TranscriptionError) as error:
                await transcribe_pcm(b'\0\0',api_key='secret-key',model='model/name',
                    transport=httpx.MockTransport(lambda _request:httpx.Response(status,content=body)))
            self.assertNotIn('secret-key',str(error.exception))
            self.assertNotIn('private audio',str(error.exception))

    async def test_http_failures_keep_only_the_service_failure_code(self):
        for status, code in ((401, 'invalid_token'), (403, 'invalid_token'), (429, 'rate_limited')):
            with self.subTest(status=status), self.assertRaises(TranscriptionError) as error:
                await transcribe_pcm(b'\0\0', api_key='test-key', model='model/name',
                    transport=httpx.MockTransport(lambda _request: httpx.Response(status, content=b'private')))
            self.assertEqual(str(error.exception), code)

    async def test_invalid_and_oversized_responses_are_rejected(self):
        for response in (
            httpx.Response(200, content=b"x" * 32769),
            httpx.Response(200, json={"text": "\x01"}),
            httpx.Response(200, content=b'{"text":"\\ud800"}'),
            httpx.Response(200, content=gzip.compress(b"{}"), headers={"content-encoding": "gzip"}),
        ):
            with self.subTest(response=response), self.assertRaises(TranscriptionError):
                await transcribe_pcm(b"\0\0", api_key="test-key", model="model/name",
                                     transport=httpx.MockTransport(lambda _request: response))

    async def test_text_matches_device_rules_at_boundaries(self):
        # JSON may escape every character even when the resulting text is short.
        escaped = b'{"text":"' + b"\\u0061" * 4096 + b'"}'
        text = await transcribe_pcm(b"\0\0", api_key="test-key", model="model/name",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=escaped)))
        self.assertEqual(text, "a" * 4096)
        with self.assertRaises(TranscriptionError):
            await transcribe_pcm(b"\0\0", api_key="test-key", model="model/name",
                transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"text": "a\x7fb"})))

    async def test_network_failure_is_sanitized(self):
        def fail(request):
            raise httpx.ConnectError("secret-key", request=request)
        with self.assertRaises(TranscriptionError) as error:
            await transcribe_pcm(b"\0\0", api_key="secret-key", model="model/name",
                                 transport=httpx.MockTransport(fail))
        self.assertNotIn("secret-key", str(error.exception))

    async def test_explicit_recording_is_transcribed_then_queued_on_device(self):
        client = type("Client", (), {})()
        client.queue_transcript = AsyncMock()
        client.channel = type("Channel", (), {"capabilities": {"transcript_display": 1}})()
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"text": "合成结果"}))
        await transcribe_to_device(client, b"\0\0", api_key="test-key", model="model/name", transport=transport)
        client.queue_transcript.assert_awaited_once_with("合成结果")
        client.queue_transcript.reset_mock()
        client.channel.capabilities['capture_transcript_ticket']=1
        await transcribe_to_device(client, b"\0\0", api_key="test-key", model="model/name", transport=transport, capture_ticket=7)
        client.queue_transcript.assert_awaited_once_with("合成结果",capture_ticket=7)
        client.queue_transcript.reset_mock()
        transport = httpx.MockTransport(lambda _request: httpx.Response(500))
        with self.assertRaises(TranscriptionError):
            await transcribe_to_device(client, b"\0\0", api_key="test-key", model="model/name", transport=transport)
        client.queue_transcript.assert_not_awaited()
        client.channel.capabilities = {}
        def forbidden(_request):
            raise AssertionError("unsupported device must not upload")
        with self.assertRaises(DeviceError):
            await transcribe_to_device(client, b"\0\0", api_key="test-key", model="model/name",
                                       transport=httpx.MockTransport(forbidden))

    async def test_cancellation_stops_request(self):
        entered,closed=asyncio.Event(),asyncio.Event()
        async def handler(_request):
            entered.set()
            try:await asyncio.Future()
            finally:closed.set()
        task=asyncio.create_task(transcribe_pcm(b'\0\0',api_key='test-key',model='model/name',transport=httpx.MockTransport(handler)))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(closed.is_set())


if __name__=='__main__':unittest.main()
