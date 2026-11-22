import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from pydantic import ValidationError
from deskpet_platform.voice_tools import build_voice_tools, voice_tool_supported
from deskpet_platform.voice_agent import VoiceAgent


class VoiceToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session=type('Session',(),{})()
        self.session.get_status=AsyncMock(return_value={'connection':'connected','page':0,'screen_page':'role',
                                                       'role':{'id':'user-whale','state':'ready'},'private':'omit'})
        self.session.list_actions=AsyncMock(return_value={'status':'ok','actions':['idle','touched']})
        self.session.play_role=AsyncMock(return_value={'status':'started','action':'touched','operation':7,'boot':9})
        self.session.set_view=AsyncMock(return_value={'status':'shown','page':'home'})

    async def test_only_explicitly_authorized_tools_are_available(self):
        tools=build_voice_tools(self.session,{'device_status','list_role_actions'})
        self.assertEqual({tool.name for tool in tools},{'device_status','list_role_actions'})
        with self.assertRaises(ValueError):build_voice_tools(self.session,{'unknown'})
        status=await next(t for t in tools if t.name=='device_status').invoke()
        self.assertTrue(status.ok);self.assertEqual(status.value['connection'],'connected')
        self.assertNotIn('private',status.value)
        self.assertNotIn('page',status.value)
        self.assertEqual(status.value['screen_page'],'role')

    async def test_revoked_permission_is_checked_again_before_execution(self):
        enabled={'play_role_action'}
        tool=build_voice_tools(self.session,enabled,authorize=lambda name:name in enabled)[0]
        enabled.clear()
        result=await tool.invoke(action='touched',duration_ms=None)
        self.assertFalse(result.ok);self.assertEqual(result.value['status'],'not_authorized')
        self.session.play_role.assert_not_awaited()
        failing=build_voice_tools(self.session,{'device_status'},authorize=lambda _:1/0)[0]
        self.assertFalse((await failing.invoke()).ok);self.session.get_status.assert_not_awaited()

    async def test_permission_change_waits_for_an_inflight_action(self):
        lock=asyncio.Lock();enabled={'play_role_action'};entered=asyncio.Event();finish=asyncio.Event()
        async def play(*_args):entered.set();await finish.wait();return {'status':'started'}
        self.session.play_role=AsyncMock(side_effect=play)
        tool=build_voice_tools(self.session,enabled,authorize=lambda name:name in enabled,permission_lock=lambda:lock)[0]
        task=asyncio.create_task(tool.invoke(action='touched',duration_ms=None));await entered.wait()
        async def revoke():
            async with lock:enabled.clear()
        revocation=asyncio.create_task(revoke());await asyncio.sleep(0)
        self.assertFalse(revocation.done());finish.set()
        self.assertTrue((await task).ok);await revocation
        self.assertFalse((await tool.invoke(action='touched',duration_ms=None)).ok)
        self.assertEqual(self.session.play_role.await_count,1)

    async def test_busy_and_unsupported_actions_are_failures_not_success(self):
        tool=build_voice_tools(self.session,{'play_role_action'})[0]
        result=await tool.invoke(action='touched',duration_ms=None)
        self.assertTrue(result.ok);self.assertFalse(result.completed);self.assertEqual(result.value['status'],'started')
        self.session.play_role.return_value={'status':'busy','action':'touched'}
        self.assertFalse((await tool.invoke(action='touched',duration_ms=None)).ok)
        self.session.play_role.return_value={'status':'unsupported','action':'touched'}
        self.assertFalse((await tool.invoke(action='touched',duration_ms=None)).ok)

    async def test_open_page_is_temporary_and_strictly_validated(self):
        tool=build_voice_tools(self.session,{'open_page'})[0]
        self.assertEqual(tool.parameters.model_validate({'page':'home'},strict=True).page,'home')
        with self.assertRaises(ValidationError):tool.parameters.model_validate({'page':'danger'},strict=True)
        result=await tool.invoke(page='home')
        self.assertTrue(result.ok);self.session.set_view.assert_awaited_once_with('home',False,None)
        self.session.set_view.return_value={'status':'not_connected'}
        self.assertFalse((await tool.invoke(page='home')).ok)

    async def test_offline_action_list_returns_failure(self):
        self.session.list_actions.return_value={'status':'not_connected','connection':{'private':'omit'}}
        tool=build_voice_tools(self.session,{'list_role_actions'})[0]
        result=await tool.invoke();self.assertFalse(result.ok)
        self.assertNotIn('connection',result.value)

    async def test_agent_uses_actual_action_list_and_reports_busy(self):
        self.session.play_role.return_value={'status':'busy','action':'touched'}
        def model_call(name,arguments,identity):
            return {'role':'assistant','content':None,'tool_calls':[{'id':identity,'type':'function',
                'function':{'name':name,'arguments':arguments}}]}
        backend=AsyncMock()
        backend.complete.side_effect=[model_call('list_role_actions','{}','one'),
            model_call('play_role_action','{"action":"touched"}','two'),
            {'role':'assistant','content':'动作已经播放完了。'}]
        agent=VoiceAgent(backend,build_voice_tools(self.session,{'list_role_actions','play_role_action'}))
        result=await agent.turn('让它做摸摸的互动')
        self.session.list_actions.assert_awaited_once_with()
        self.session.play_role.assert_awaited_once_with('touched',None)
        self.assertEqual([event.status for event in result.events],['ok','failed'])
        self.assertEqual(result.text,'设备正忙，动作未开始。')

    async def test_started_action_is_not_described_as_finished(self):
        backend=AsyncMock();backend.complete.side_effect=[
            {'role':'assistant','content':None,'tool_calls':[{'id':'one','type':'function',
                'function':{'name':'play_role_action','arguments':'{"action":"touched"}'}}]},
            {'role':'assistant','content':'动作已经完成了。'}]
        agent=VoiceAgent(backend,build_voice_tools(self.session,{'play_role_action'}))
        result=await agent.turn('做摸摸动作')
        self.assertEqual(result.events[-1].status,'started')
        self.assertEqual(result.text,'操作已开始，尚未确认完成。')

    async def test_media_tools_require_ticket_capability_and_validate_narrow_actions(self):
        tools = {tool.name: tool for tool in build_voice_tools(self.session, {'music_control', 'photo_slideshow'})}
        music, photo = tools['music_control'].parameters, tools['photo_slideshow'].parameters
        self.assertFalse(voice_tool_supported('music_control', {'music_control': 1}))
        self.assertFalse(voice_tool_supported('photo_slideshow', {'photo_album': 1}))
        self.assertTrue(voice_tool_supported('music_control', {'music_control': 1, 'capture_media_tools': 1}))
        self.assertTrue(voice_tool_supported('photo_slideshow', {'photo_album': 1, 'capture_media_tools': 1}))
        self.assertEqual(music.model_validate({'action': 'play', 'path': 'music/海.flac'}, strict=True).path,
                         'music/海.flac')
        self.assertEqual(photo.model_validate({'mode': 'interval', 'interval_seconds': 15}, strict=True).interval_seconds, 15)
        for fields in ({'action': 'delete'}, {'action': 'next', 'path': 'music/海.flac'},
                       {'action': 'play', 'path': 'music/../海.flac'}, {'action': 'stop', 'ticket': 2}):
            with self.assertRaises(ValidationError):
                music.model_validate(fields, strict=True)
        for fields in ({'mode': 'grid'}, {'mode': 'interval', 'interval_seconds': 30},
                       {'mode': 'interval'}, {'mode': 'pause', 'interval_seconds': 10}):
            with self.assertRaises(ValidationError):
                photo.model_validate(fields, strict=True)

    async def test_media_receipt_is_deferred_until_the_board_finishes_speaking(self):
        self.session.voice_music_control = AsyncMock(return_value={
            'status': 'ok', 'queued': True, 'target_path': 'music/海.flac', 'state': 'stopped',
        })
        backend = AsyncMock()
        backend.complete.side_effect = [
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'media', 'type': 'function',
                'function': {'name': 'music_control', 'arguments': '{"action":"play","path":"music/海.flac"}'}}]},
            {'role': 'assistant', 'content': '正在为你播放。'},
        ]
        agent = VoiceAgent(backend, build_voice_tools(self.session, {'music_control'}))
        turn = await agent.turn('在搭搭播放海')
        self.session.voice_music_control.assert_awaited_once_with('play', 'music/海.flac')
        self.assertEqual(turn.events[-1].status, 'deferred')
        self.assertEqual(turn.text, '已排队，实际播放状态以搭搭显示为准。')

    async def test_unknown_mutation_stops_the_turn_before_the_model_can_repeat_it(self):
        cases = (
            ('device_settings', 'voice_device_settings', {'volume': 35, 'brightness': 65},
             {'status': 'unknown', 'reason': 'settings_partial'}, '设备可能已应用部分设置，请先核对音量和亮度。'),
            ('device_restart', 'voice_restart', {}, {'status': 'unknown'}, '重启请求结果未知，请先核对设备状态。'),
            ('photo_slideshow', 'voice_photo_slideshow', {'mode': 'pause'},
             {'status': 'unknown'}, '轮播操作结果未知，请先核对相册状态。'),
        )
        for name, method, arguments, response, expected_text in cases:
            with self.subTest(tool=name):
                invoke = AsyncMock(return_value=response)
                setattr(self.session, method, invoke)
                backend = AsyncMock()
                backend.complete.side_effect = [
                    {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': identity, 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(arguments)},
                    }]}
                    for identity in ('first', 'repeat')
                ] + [{'role': 'assistant', 'content': '操作完成。'}]
                agent = VoiceAgent(backend, build_voice_tools(self.session, {name}))
                result = await agent.turn('执行一次操作')
                backend.complete.assert_awaited_once()
                if name == 'photo_slideshow':
                    invoke.assert_awaited_once_with('pause', None)
                else:
                    invoke.assert_awaited_once_with(**arguments)
                self.assertEqual(result.text, expected_text)
                self.assertEqual([event.status for event in result.events], ['unknown'])
                self.assertEqual(json.loads(agent.messages[-2]['content'])['status'], 'unknown')
                self.assertEqual(agent.messages[-1], {'role': 'assistant', 'content': expected_text})
                backend.complete.side_effect = None
                backend.complete.return_value = {'role': 'assistant', 'content': '请告诉我接下来要做什么。'}
                next_turn = await agent.turn('我们先聊点别的')
                self.assertEqual(next_turn.events, ())
                invoke.assert_awaited_once()
