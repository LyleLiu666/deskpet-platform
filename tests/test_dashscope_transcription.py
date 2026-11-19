import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from deskpet_platform import transcription


class DashScopeTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def test_requires_explicit_and_exclusive_provider_configuration(self):
        key = {'DASHSCOPE_API_KEY': 'test-key'}
        config = {**key, 'DESKPET_ENABLE_DASHSCOPE_TRANSCRIPTION': '1'}
        self.assertIsNone(transcription.configured_service(key))
        self.assertIsNone(transcription.configured_service({
            **config, 'DESKPET_ENABLE_LOCAL_TRANSCRIPTION': '1',
        }))
        self.assertIsNone(transcription.configured_service({
            **config, 'DASHSCOPE_WORKSPACE_ID': 'bad.example',
        }))
        service = transcription.configured_service({
            **config, 'DASHSCOPE_WORKSPACE_ID': 'llm-123',
        })
        self.assertIsInstance(service, transcription.DashScopeTranscription)
        self.assertNotIn('test-key', repr(service))

    async def test_sends_pcm_and_returns_only_final_sentences(self):
        transcript, socket, session = await self.recognize([
            ('result-generated', {'text': '中间结果', 'sentence_end': False}),
            ('result-generated', {'heartbeat': True}),
            ('result-generated', {'text': '语音检查。', 'sentence_end': True}),
            ('task-finished', None),
        ])
        self.assertEqual(transcript, '语音检查。')
        start, finish = [call.args[0] for call in socket.send_json.await_args_list]
        self.assertEqual(start['payload']['model'], 'paraformer-realtime-v2')
        self.assertEqual(start['payload']['parameters']['sample_rate'], 24000)
        self.assertEqual(finish['header']['action'], 'finish-task')
        self.assertEqual(finish['header']['task_id'], start['header']['task_id'])
        socket.send_bytes.assert_awaited_once_with(b'\x01\x00')
        self.assertEqual(session.ws_connect.call_args.args[0],
                         'wss://llm-123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference')

    async def test_no_final_sentence_is_reported_as_no_speech(self):
        with self.assertRaisesRegex(transcription.TranscriptionError, '^no_speech$'):
            await self.recognize([('task-finished', None)])

    async def test_failed_task_returns_sanitized_error(self):
        with self.assertRaisesRegex(transcription.TranscriptionError, '^service_error$'):
            await self.recognize([('task-failed', None)])

    async def recognize(self, events):
        socket = AsyncMock()
        messages = iter([('task-started', None), *events])

        def receive():
            event, sentence = next(messages)
            task_id = socket.send_json.await_args_list[0].args[0]['header']['task_id']
            payload = {} if sentence is None else {'output': {'sentence': sentence}}
            return {'header': {'event': event, 'task_id': task_id}, 'payload': payload}

        socket.receive_json.side_effect = receive
        session = MagicMock()
        session.ws_connect.return_value.__aenter__.return_value = socket
        with patch('aiohttp.ClientSession') as create_session:
            create_session.return_value.__aenter__.return_value = session
            transcript = await transcription.transcribe_dashscope_pcm(
                b'\x01\x00', transcription.DashScopeTranscription('test-key', 'llm-123'))
        return transcript, socket, session
