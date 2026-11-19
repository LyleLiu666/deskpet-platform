import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.voice_agent import AgentBusy, AgentError, AgentResult, Confirmation, VoiceAgent
from deskpet_platform.voice_conversation import ConversationBusy, VoiceConversation, create_device_conversation


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_device_names_are_loaded_for_conversation_and_not_treated_as_tools(self):
        session=type('Session',(),{})()
        session.voice_names=AsyncMock(return_value={'status':'ok','user_name':'小刘','device_name':'小搭'})
        session.speak_voice_reply=AsyncMock(return_value={'status':'finished'})
        backend=type('Backend',(),{})()
        backend.complete=AsyncMock(return_value={'role':'assistant','content':'你好'})
        conversation=await create_device_conversation(session,backend,enabled_tools=set())
        await conversation.turn('你好')
        messages,tools=backend.complete.await_args.args
        self.assertEqual(tools,[])
        self.assertIn(json.dumps({'user_name':'小刘','device_name':'小搭'},ensure_ascii=False),messages[0]['content'])
        self.assertIn('仅作为称呼数据',messages[0]['content'])
        await conversation.end()
        await conversation.turn('再见')
        self.assertIn('小搭',backend.complete.await_args.args[0][0]['content'])

    async def test_device_names_failure_does_not_silently_invent_names(self):
        session=type('Session',(),{})()
        session.voice_names=AsyncMock(return_value={'status':'not_connected'})
        session.speak_voice_reply=AsyncMock()
        backend=AsyncMock()
        with self.assertRaises(AgentError) as error:
            await create_device_conversation(session,backend,enabled_tools=set())
        self.assertEqual(error.exception.code,'voice_names_not_connected')
        backend.complete.assert_not_awaited()
        session.voice_names.return_value={'status':[]}
        with self.assertRaises(AgentError) as error:
            await create_device_conversation(session,backend,enabled_tools=set())
        self.assertEqual(error.exception.code,'voice_names_invalid_response')
    async def test_real_agent_reply_passes_through_board_speech(self):
        with TemporaryDirectory() as directory:
            board = BoardSession(Path(directory)/'device.json', no_token_probe=True)
            client = type('Board', (), {})()
            client.channel = type('Channel', (), {'capabilities': {'speech_pcm': 1}})()
            client.play_speech_pcm = AsyncMock(return_value={'status': 'finished', 'duration_ms': 1})
            board.client = client
            backend = type('Backend', (), {})()
            backend.complete = AsyncMock(return_value={'role': 'assistant', 'content': '你好，\n今天怎么样？'})
            conversation = VoiceConversation(VoiceAgent(backend, []), board.speak_voice_reply)
            async def synth(text):return text.encode()
            with patch('deskpet_platform.mcp_server.synthesize_speech', side_effect=synth):
                result = await conversation.turn('你好')
            self.assertEqual(result.playback['status'], 'finished')
            self.assertEqual(b''.join(c.args[0] for c in client.play_speech_pcm.await_args_list).decode(),
                             '你好， 今天怎么样？')
            await conversation.end()

    def make_conversation(self):
        agent = type('Agent', (), {})()
        agent.turn = AsyncMock(return_value=AgentResult(text='你好'))
        agent.confirm = AsyncMock(return_value=AgentResult(text='完成'))
        agent.end = AsyncMock()
        speak = AsyncMock(return_value={'status': 'finished', 'duration_ms': 10})
        return VoiceConversation(agent, speak), agent, speak

    async def test_turn_reads_reply_and_returns_results(self):
        conversation, agent, speak = self.make_conversation()
        result = await conversation.turn('你好')
        agent.turn.assert_awaited_once_with('你好')
        speak.assert_awaited_once_with('你好')
        self.assertEqual(result.agent.text, '你好')
        self.assertEqual(result.playback['status'], 'finished')

    async def test_confirmation_must_be_explicit_and_is_returned_to_caller(self):
        conversation, agent, speak = self.make_conversation()
        confirmation = Confirmation('id', 'action', '确认操作', {})
        agent.turn.return_value = AgentResult(confirmation=confirmation)
        result = await conversation.turn('执行')
        self.assertEqual(result.agent.confirmation, confirmation)
        self.assertIsNone(result.playback)
        agent.confirm.assert_not_awaited()
        speak.assert_not_awaited()
        result = await conversation.confirm('id', False)
        agent.confirm.assert_awaited_once_with('id', False)
        self.assertEqual(result.agent.text, '完成')
        speak.assert_awaited_once_with('完成')

    async def test_playback_failure_is_visible_alongside_agent_result(self):
        conversation, agent, speak = self.make_conversation()
        speak.return_value = {'status': 'not_connected'}
        result = await conversation.turn('你好')
        self.assertEqual(result.agent.text, '你好')
        self.assertEqual(result.playback['status'], 'not_connected')

    async def test_end_interrupts_reply_and_clears_conversation(self):
        conversation, agent, speak = self.make_conversation()
        entered = asyncio.Event()
        async def blocking_speech(_text):
            entered.set()
            await asyncio.Event().wait()
        speak.side_effect = blocking_speech
        task = asyncio.create_task(conversation.turn('你好'))
        await entered.wait()
        with self.assertRaises(ConversationBusy):
            await conversation.turn('第二句')
        await conversation.end()
        with self.assertRaises(asyncio.CancelledError):
            await task
        agent.end.assert_awaited_once()
        speak.side_effect = None
        self.assertEqual((await conversation.turn('新的对话')).agent.text, '你好')

    async def test_end_interrupts_pending_model_request(self):
        conversation, agent, speak = self.make_conversation()
        entered = asyncio.Event()
        async def blocking_model(_text):
            entered.set()
            await asyncio.Event().wait()
        agent.turn.side_effect = blocking_model
        task = asyncio.create_task(conversation.turn('你好'))
        await entered.wait()
        await conversation.end()
        with self.assertRaises(asyncio.CancelledError):
            await task
        speak.assert_not_awaited()
        agent.end.assert_awaited_once()

    async def test_external_cancellation_requires_end_before_reuse(self):
        conversation, agent, speak = self.make_conversation()
        entered = asyncio.Event()
        async def blocking_model(_text):
            entered.set()
            await asyncio.Event().wait()
        agent.turn.side_effect = blocking_model
        task = asyncio.create_task(conversation.turn('你好'))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ConversationBusy):
            await conversation.turn('下一句')
        await conversation.end()
        agent.turn.side_effect = None
        self.assertEqual((await conversation.turn('新的对话')).agent.text, '你好')

    async def test_model_failure_requires_history_reset(self):
        conversation, agent, _ = self.make_conversation()
        agent.turn.side_effect = RuntimeError('model unavailable')
        with self.assertRaises(RuntimeError):
            await conversation.turn('你好')
        with self.assertRaises(ConversationBusy):
            await conversation.turn('下一句')
        await conversation.end()
        agent.turn.side_effect = None
        self.assertEqual((await conversation.turn('新的对话')).agent.text, '你好')

    async def test_confirmation_mistakes_do_not_discard_pending_action(self):
        conversation, agent, _ = self.make_conversation()
        agent.confirm.side_effect = AgentError('unknown_confirmation')
        with self.assertRaises(AgentError):
            await conversation.confirm('wrong', True)
        agent.confirm.side_effect = None
        agent.turn.side_effect = AgentBusy('confirmation_pending')
        with self.assertRaises(AgentBusy):
            await conversation.turn('额外一句')
        self.assertEqual((await conversation.confirm('correct', False)).agent.text, '完成')
