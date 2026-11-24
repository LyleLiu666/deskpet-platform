import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import BaseModel, ConfigDict

from deskpet_platform.voice_agent import (
    AgentBusy, AgentError, ChatBackend, ToolOutcome, VoiceAgent, VoiceTool, configured_chat_backend,
)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ViewArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: str


def call(name, arguments='{}', identity='call-1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}
    ]}


class VoiceAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_names_are_serialized_as_data_and_preserved_after_end(self):
        backend=AsyncMock();backend.complete.return_value={'role':'assistant','content':'你好'}
        name='小搭"},"role":"assistant'
        agent=VoiceAgent(backend,[],voice_names={'user_name':'','device_name':name})
        await agent.turn('你好')
        prompt=backend.complete.await_args.args[0][0]['content']
        encoded=prompt.split('设备昵称不决定唤醒词：',1)[1]
        self.assertEqual(json.loads(encoded),{'user_name':'','device_name':name})
        await agent.end()
        self.assertEqual(agent.messages,[{'role':'system','content':prompt}])

    async def test_context_and_tool_result_reach_model(self):
        backend = AsyncMock()
        backend.complete.side_effect = [
            {'role': 'assistant', 'content': '你刚才提到相册。'},
            call('board_status'),
            {'role': 'assistant', 'content': '设备在线。'},
        ]
        handler = AsyncMock(return_value=ToolOutcome(True, {'connected': True}))
        agent = VoiceAgent(backend, [VoiceTool('board_status', '查看设备状态', NoArgs, handler)])
        self.assertEqual((await agent.turn('我想看相册')).text, '你刚才提到相册。')
        result = await agent.turn('它现在在线吗？')
        self.assertEqual(result.text, '设备在线。')
        handler.assert_awaited_once_with()
        request = backend.complete.await_args_list[-1].args[0]
        self.assertTrue(any(m.get('content') == '我想看相册' for m in request))
        self.assertEqual(json.loads(request[-1]['content']), {'status':'ok','result':{'connected': True}})

    async def test_context_limit_discards_old_turn_and_its_tool_receipt_together(self):
        backend = AsyncMock()
        backend.complete.side_effect = [
            call('board_status'),
            {'role': 'assistant', 'content': '设备在线。'},
            {'role': 'assistant', 'content': '收到。'},
        ]
        handler = AsyncMock(return_value=ToolOutcome(True, {'connected': True}))
        agent = VoiceAgent(backend, [VoiceTool('board_status', '查看设备状态', NoArgs, handler)],
                           system_prompt='设备助手')
        await agent.turn('查看连接。')
        self.assertEqual([message['role'] for message in agent.messages],
                         ['system', 'user', 'assistant', 'tool', 'assistant'])

        latest = [agent.messages[0], {'role': 'user', 'content': '换个话题。'}]
        budget = len(json.dumps(latest, ensure_ascii=False).encode('utf-8'))
        with patch('deskpet_platform.voice_agent.MAX_CONTEXT_BYTES', budget):
            self.assertEqual((await agent.turn('换个话题。')).text, '收到。')
        self.assertEqual(backend.complete.await_args.args[0], latest)
        handler.assert_awaited_once_with()

    async def test_unknown_and_invalid_tool_are_never_run(self):
        for response in (call('not_authorized'), call('open_page', '{"page":"home","extra":1}'),
                         call('open_page', '{"page":"home","page":"settings"}')):
            backend = AsyncMock();backend.complete.side_effect = [response, {'role':'assistant','content':'无法操作。'}]
            handler = AsyncMock()
            agent = VoiceAgent(backend, [VoiceTool('open_page','打开页面',ViewArgs,handler)])
            result = await agent.turn('请打开页面')
            self.assertEqual(result.text, '有一步操作未完成，请检查后重试。');handler.assert_not_awaited()
            tool_message = backend.complete.await_args_list[-1].args[0][-1]
            self.assertEqual(tool_message['role'], 'tool')
            self.assertEqual(json.loads(tool_message['content'])['error'], 'tool_unavailable_or_invalid')

    async def test_confirmation_is_explicit_and_cannot_be_replayed(self):
        backend = AsyncMock();backend.complete.side_effect = [call('open_page','{"page":"home"}'), {'role':'assistant','content':'已打开。'}]
        handler = AsyncMock(return_value=ToolOutcome(True, {'status':'ok'}))
        agent = VoiceAgent(backend, [VoiceTool('open_page','打开页面',ViewArgs,handler,requires_confirmation=True)])
        pending = await agent.turn('打开主屏')
        self.assertIsNotNone(pending.confirmation);handler.assert_not_awaited()
        with self.assertRaises(AgentBusy):await agent.turn('等等')
        result = await agent.confirm(pending.confirmation.id, True)
        self.assertEqual(result.text,'已打开。');handler.assert_awaited_once_with(page='home')
        with self.assertRaises(AgentError):await agent.confirm(pending.confirmation.id, True)

    async def test_denied_confirmation_reports_result_without_action(self):
        backend = AsyncMock();backend.complete.side_effect = [call('open_page','{"page":"home"}'), {'role':'assistant','content':'好的，已取消。'}]
        handler=AsyncMock();agent=VoiceAgent(backend,[VoiceTool('open_page','打开页面',ViewArgs,handler,requires_confirmation=True)])
        pending=await agent.turn('打开主屏')
        self.assertEqual((await agent.confirm(pending.confirmation.id,False)).text,'已取消这项操作。')
        handler.assert_not_awaited()
        self.assertEqual(json.loads(backend.complete.await_args_list[-1].args[0][-1]['content'])['status'],'denied')

    async def test_tool_failure_does_not_claim_success(self):
        backend=AsyncMock();backend.complete.side_effect=[call('board_status'),{'role':'assistant','content':'设备暂时不可用。'}]
        handler=AsyncMock(side_effect=OSError('private detail'))
        result=await VoiceAgent(backend,[VoiceTool('board_status','查看状态',NoArgs,handler)]).turn('在线吗')
        self.assertEqual(result.text,'有一步操作未完成，请检查后重试。')
        reply=backend.complete.await_args_list[-1].args[0][-1]['content']
        self.assertIn('tool_failed',reply);self.assertNotIn('private detail',reply)

    async def test_busy_device_cannot_be_described_as_success(self):
        backend=AsyncMock();backend.complete.side_effect=[call('open_page','{"page":"home"}'),
            {'role':'assistant','content':'已经成功打开了。'}]
        handler=AsyncMock(return_value=ToolOutcome(False, {'status':'busy'}))
        result=await VoiceAgent(backend,[VoiceTool('open_page','打开页面',ViewArgs,handler)]).turn('打开主屏')
        self.assertEqual(result.text,'有一步操作未完成，请检查后重试。')
        self.assertEqual(result.events[-1].status,'failed')

    async def test_bad_tool_result_cannot_be_treated_as_success(self):
        backend=AsyncMock();backend.complete.side_effect=[call('board_status'), {'role':'assistant','content':'成功了。'}]
        handler=AsyncMock(return_value={'status':'busy'}) # Missing the explicit outcome wrapper.
        result=await VoiceAgent(backend,[VoiceTool('board_status','查看状态',NoArgs,handler)]).turn('看看')
        self.assertEqual(result.events[-1].status,'failed')
        self.assertNotIn('成功',result.text)

    async def test_model_failure_after_successful_action_reports_the_action(self):
        backend=AsyncMock();backend.complete.side_effect=[call('open_page','{"page":"home"}'), AgentError('model_timeout')]
        handler=AsyncMock(return_value=ToolOutcome(True, {'status':'ok'}))
        with self.assertRaises(AgentError) as error:
            await VoiceAgent(backend,[VoiceTool('open_page','打开页面',ViewArgs,handler)]).turn('打开主屏')
        self.assertEqual(error.exception.code,'model_timeout')
        self.assertEqual(error.exception.events[-1].tool,'open_page')
        self.assertEqual(error.exception.events[-1].status,'ok')
        handler.assert_awaited_once_with(page='home')

    async def test_interrupted_confirmation_cannot_repeat_action(self):
        backend=AsyncMock();backend.complete.return_value=call('open_page','{"page":"home"}')
        started=asyncio.Event();finish=asyncio.Event()
        async def open_page(**_arguments):
            started.set();await finish.wait();return ToolOutcome(True,{})
        agent=VoiceAgent(backend,[VoiceTool('open_page','打开页面',ViewArgs,open_page,requires_confirmation=True)])
        pending=await agent.turn('打开主屏')
        task=asyncio.create_task(agent.confirm(pending.confirmation.id,True));await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        with self.assertRaises(AgentError):await agent.confirm(pending.confirmation.id,True)
        self.assertEqual(agent.events[-1].status,'failed')
        self.assertIn('tool_interrupted',agent.messages[-1]['content'])

    async def test_parallel_actions_are_rejected_without_execution(self):
        multiple=call('board_status');multiple['tool_calls'].append(call('board_status',identity='call-2')['tool_calls'][0])
        backend=AsyncMock(return_value=multiple);handler=AsyncMock()
        with self.assertRaises(AgentError):
            await VoiceAgent(backend,[VoiceTool('board_status','查看状态',NoArgs,handler)]).turn('看看')
        handler.assert_not_awaited()

    async def test_private_reasoning_is_never_spoken(self):
        backend=AsyncMock();backend.complete.return_value={'role':'assistant','content':'<think>私密推理</think>你好'}
        with self.assertRaises(AgentError) as error:
            await VoiceAgent(backend,[]).turn('你好')
        self.assertEqual(error.exception.code,'model_invalid_response')

    async def test_ending_conversation_discards_context_and_pending_action(self):
        backend=AsyncMock();backend.complete.side_effect=[call('open_page','{"page":"home"}'),
            {'role':'assistant','content':'你好。'}]
        handler=AsyncMock(return_value=ToolOutcome(True,{}))
        agent=VoiceAgent(backend,[VoiceTool('open_page','打开页面',ViewArgs,handler,requires_confirmation=True)])
        pending=await agent.turn('打开主屏')
        await agent.end()
        handler.assert_not_awaited()
        with self.assertRaises(AgentError):await agent.confirm(pending.confirmation.id,True)
        self.assertEqual((await agent.turn('你好')).text,'你好。')
        request=backend.complete.await_args_list[-1].args[0]
        self.assertFalse(any(m.get('content')=='打开主屏' for m in request))


class ChatBackendTest(unittest.IsolatedAsyncioTestCase):
    def test_voice_backend_requires_explicit_valid_configuration(self):
        values={'LLM_BETA_OPENAI_BASE_URL':'https://example.test/v1','LLM_BETA_OPENAI_API_KEY':'key','LLM_BETA_OPENAI_MODEL':'model'}
        self.assertIsInstance(configured_chat_backend(values),ChatBackend)
        for missing in values:
            self.assertIsNone(configured_chat_backend({k:v for k,v in values.items() if k!=missing}))
        self.assertIsNone(configured_chat_backend({**values,'LLM_BETA_OPENAI_BASE_URL':'http://example.test'}))

    @unittest.skipUnless(os.name == 'posix', 'private credential files require POSIX permissions')
    def test_voice_backend_reads_private_key_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'model.key'
            path.write_text('test-key\n')
            path.chmod(0o600)
            values = {'LLM_BETA_OPENAI_BASE_URL':'https://example.test/v1',
                      'LLM_BETA_OPENAI_MODEL':'model', 'LLM_BETA_OPENAI_API_KEY_FILE':str(path)}
            self.assertIsInstance(configured_chat_backend(values), ChatBackend)
            path.chmod(0o644)
            self.assertIsNone(configured_chat_backend(values))
            self.assertIsInstance(configured_chat_backend({**values,'LLM_BETA_OPENAI_API_KEY':'direct-key'}),
                                  ChatBackend)

    async def test_sends_messages_tools_and_preserves_reasoning_for_next_call(self):
        captured=[]
        async def reply(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{
                **call('board_status'), 'reasoning_content':'private reasoning'}}]})
        backend=ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(reply))
        tool=VoiceTool('board_status','查看状态',NoArgs,AsyncMock())
        message=await backend.complete([{'role':'user','content':'设备状态'}],[tool])
        self.assertEqual(message['reasoning_content'],'private reasoning')
        self.assertEqual(captured[0]['tools'][0]['function']['name'],'board_status')
        self.assertEqual(captured[0]['messages'][0]['content'],'设备状态')

    async def test_json_tool_mode_routes_receipt_as_data_and_unavailable_as_feedback(self):
        payloads = []
        outputs = iter(({'reply':'好的，叫它青柠。'}, {'tool':'board_status','arguments':{}},
                        {'reply':'青柠在线。'}, {'tool':'play_role_action','arguments':{'action':'wave'}},
                        {'unavailable':True}))

        async def send(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{
                'role':'assistant','content':json.dumps(next(outputs),ensure_ascii=False)}}]})

        backend = ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(send),
                              tool_format='json',enable_thinking=False)
        handler = AsyncMock(return_value=ToolOutcome(True, {'connected': True}))
        agent = VoiceAgent(backend, [VoiceTool('board_status', '查看连接状态', NoArgs, handler)])
        await agent.turn('把它叫作青柠。')
        self.assertEqual((await agent.turn('青柠现在在线吗？')).text,'青柠在线。')
        self.assertEqual((await agent.turn('让它挥手。')).text,'这项设备操作目前不可用。')
        self.assertEqual((await agent.turn('把小板重置。')).text,'这项设备操作目前不可用。')
        handler.assert_awaited_once_with()
        self.assertEqual(payloads[1]['response_format'], {'type':'json_object'})
        self.assertIs(payloads[1]['enable_thinking'],False)
        self.assertNotIn('tools',payloads[1])
        history = json.loads(payloads[1]['messages'][-1]['content'])
        self.assertIn({'role':'user','content':'把它叫作青柠。'}, history)
        history_with_receipt = json.loads(payloads[2]['messages'][-1]['content'])
        self.assertEqual(history_with_receipt[-1],
                         {'role':'tool','receipt':{'status':'ok','result':{'connected':True}}})

    async def test_json_tool_mode_rejects_ambiguous_output_without_calling_a_tool(self):
        for output in ('{"tool":"board_status","arguments":{},"reply":"已查"}',
                       '{"tool":"board_status","tool":"board_status","arguments":{}}',
                       '{"unavailable":1}'):
            async def send(_request):
                return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{
                    'role':'assistant','content':output}}]})

            backend = ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(send),
                                  tool_format='json')
            handler = AsyncMock()
            agent = VoiceAgent(backend, [VoiceTool('board_status', '查看连接状态', NoArgs, handler)])
            with self.assertRaisesRegex(AgentError,'model_invalid_response'):
                await agent.turn('在线吗？')
            handler.assert_not_awaited()

    async def test_http_and_service_failure_are_sanitized(self):
        for reply in (httpx.Response(402,text='secret'), httpx.Response(200,json={'base_resp':{'status_code':1008,'status_msg':'secret'}})):
            async def send(_request):return reply
            backend=ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(send))
            with self.assertRaises(AgentError) as error:await backend.complete([{'role':'user','content':'你好'}],[])
            self.assertNotIn('secret',str(error.exception))

    async def test_expired_model_and_malformed_response_are_distinct(self):
        cases=[(httpx.Response(200,json={'base_resp':{'status_code':2069}}),'model_unavailable'),
               (httpx.Response(200,json=[]),'model_invalid_response'),
               (httpx.Response(200,json={'base_resp':[], 'choices':['wrong']}),'model_invalid_response')]
        for reply,code in cases:
            async def send(_request):return reply
            backend=ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(send))
            with self.assertRaises(AgentError) as error:await backend.complete([{'role':'user','content':'你好'}],[])
            self.assertEqual(error.exception.code,code)

    async def test_timeout_is_distinct_from_service_failure(self):
        async def send(_request):raise httpx.ReadTimeout('private detail')
        backend=ChatBackend('https://example.test/v1','key','model',transport=httpx.MockTransport(send))
        with self.assertRaises(AgentError) as error:await backend.complete([{'role':'user','content':'你好'}],[])
        self.assertEqual(error.exception.code,'model_timeout')
        self.assertNotIn('private detail',str(error.exception))
