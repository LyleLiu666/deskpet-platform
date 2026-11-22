import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from deskpet_platform.agent_tool_callback import valid_callback_result
from deskpet_platform.channel import Channel, CommandRejected, DeviceError
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig


CAPABILITIES = {'cap_ext': 1, 'assistant_onboard': 1, 'agent_tool_callback': 1, 'role_actions': 1}


class OnboardToolCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'device.json'
        self.session = BoardSession(self.path, no_token_probe=True)
        self.config = DeviceConfig('00:12:34:56:78:9a', b'k' * 32)
        self.config.save(self.path)
        self.client = SimpleNamespace(
            config=self.config, device_id=None, link=SimpleNamespace(wireless=True),
            channel=SimpleNamespace(capabilities=CAPABILITIES.copy(), epoch='test', boot=7, online=True),
            send_voice_permissions=AsyncMock(), poll_agent_tool=AsyncMock(return_value=self.request()),
            status=AsyncMock(return_value={'role_id': 'hello', 'role_state': 'playing', 'crash_pc': 20}),
            list_actions=AsyncMock(), send_agent_tool_result=AsyncMock(),
        )
        self.session.client = self.client
        self.session.voice_permissions.update(['device_status', 'list_role_actions'])

    @staticmethod
    def request(call_id=1, name='device_status'):
        return {'ticket': 7, 'id': call_id, 'name': name}

    async def refresh(self):
        await self.session.refresh_onboard_tool_callback(self.client)

    async def test_sync_precedes_poll_and_reuses_filtered_status(self):
        async def poll():
            self.client.send_voice_permissions.assert_awaited_once_with({'device_status', 'list_role_actions'})
            return self.request()

        self.session.capture_service = None
        self.client.poll_agent_tool.side_effect = poll
        await self.refresh()
        self.client.send_agent_tool_result.assert_awaited_once_with(
            7, 1, True, {'connection': 'connected', 'role': {'id': 'hello', 'state': 'playing'}},
        )

    async def test_unpaired_transport_and_missing_capabilities_do_not_poll(self):
        self.client.link.wireless = False
        await self.refresh()
        self.client.link.wireless = True
        for name in ('cap_ext', 'assistant_onboard', 'agent_tool_callback'):
            with self.subTest(capability=name):
                self.client.channel.capabilities.pop(name)
                await self.refresh()
                self.client.channel.capabilities[name] = 1
        self.client.send_voice_permissions.side_effect = CommandRejected('busy')
        await self.refresh()
        self.client.poll_agent_tool.assert_not_awaited()

    async def test_callback_is_consumed_once_even_if_result_expires(self):
        self.client.poll_agent_tool.side_effect = [self.request(number) for number in (2, 2, 1, 3)]
        self.client.send_agent_tool_result.side_effect = CommandRejected('callback_stale')
        await self.refresh()
        self.client.channel.epoch = 'reconnect'
        for _ in range(3):
            await self.refresh()
        self.assertEqual(self.client.status.await_count, 2)
        self.assertEqual([entry.args[1] for entry in self.client.send_agent_tool_result.await_args_list], [2, 3])

    async def test_callback_ids_wrap_and_reset_on_board_reboot(self):
        self.client.poll_agent_tool.side_effect = [self.request(number) for number in (0xffffffff, 1, 1)]
        await self.refresh()
        await self.refresh()
        self.client.channel.boot += 1
        await self.refresh()
        self.assertEqual(self.client.status.await_count, 3)
        self.assertEqual(self.client.send_agent_tool_result.await_count, 3)

    async def test_revocation_after_poll_prevents_read(self):
        async def poll():
            self.session.voice_permissions.update([])
            return self.request()

        self.client.poll_agent_tool.side_effect = poll
        await self.refresh()
        self.client.status.assert_not_awaited()
        self.client.send_agent_tool_result.assert_awaited_once_with(7, 1, False, {'status': 'not_authorized'})

    async def test_revocation_during_read_prevents_forwarding_data(self):
        async def read():
            self.session.voice_permissions.update([])
            return {'role_id': 'hello'}

        self.client.status.side_effect = read
        await self.refresh()
        self.client.send_agent_tool_result.assert_awaited_once_with(7, 1, False, {'status': 'not_authorized'})

    async def test_old_connection_cannot_receive_result(self):
        async def read():
            self.client.channel.epoch = 'new-connection'
            return {'role_id': 'hello'}

        self.client.status.side_effect = read
        await self.refresh()
        self.client.status.assert_awaited_once()
        self.client.send_agent_tool_result.assert_not_awaited()

    async def test_old_pairing_cannot_receive_result(self):
        async def read():
            DeviceConfig(self.config.identity, b'new pair'.ljust(32, b'x')).save(self.path)
            return {'role_id': 'hello'}

        self.client.status.side_effect = read
        await self.refresh()
        self.client.send_agent_tool_result.assert_not_awaited()

    async def test_large_action_list_is_explicitly_truncated(self):
        actions = [('action_' * 6) + str(index) for index in range(48)]
        self.client.poll_agent_tool.return_value = self.request(name='list_role_actions')
        self.client.list_actions.return_value = actions
        await self.refresh()
        ticket, callback, success, result = self.client.send_agent_tool_result.await_args.args
        self.assertEqual((ticket, callback, success), (7, 1, True))
        self.assertTrue(valid_callback_result(result))
        self.assertIs(result['truncated'], True)
        self.assertGreater(len(result['actions']), 0)
        self.assertLess(len(result['actions']), len(actions))
        self.assertEqual(result['actions'], actions[:len(result['actions'])])

    async def test_authorized_tool_without_required_capability_reports_unsupported(self):
        self.client.channel.capabilities.pop('role_actions')
        self.client.poll_agent_tool.return_value = self.request(name='list_role_actions')
        await self.refresh()
        self.client.list_actions.assert_not_awaited()
        self.client.send_agent_tool_result.assert_awaited_once_with(7, 1, False, {'status': 'unsupported'})


class OnboardToolCallbackWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client.__new__(Client)
        self.client._operation = asyncio.Lock()
        self.client.link = SimpleNamespace(wireless=True)
        self.client.channel = SimpleNamespace(capabilities=CAPABILITIES.copy(), send=AsyncMock())

    async def test_command_shapes(self):
        send = self.client.channel.send
        offer = {'ticket': 7, 'id': 42, 'name': 'device_status'}
        send.side_effect = [{}, {'call': offer}, {}]
        self.assertIsNone(await self.client.poll_agent_tool())
        self.assertEqual(await self.client.poll_agent_tool(), offer)
        await self.client.send_agent_tool_result(7, 42, True, {'connection': 'connected'})
        self.assertEqual(send.await_args_list, [
            call('agent_tool_poll'), call('agent_tool_poll'),
            call('agent_tool_result', ticket=7, id=42, success=True, result={'connection': 'connected'}),
        ])

    async def test_malformed_call_is_rejected(self):
        correct = {'ticket': 7, 'id': 42, 'name': 'device_status'}
        for candidate in (None, {**correct, 'arguments': {}}, {**correct, 'ticket': True},
                          {**correct, 'id': 2 ** 32}, {**correct, 'name': 'device_restart'}):
            with self.subTest(candidate=candidate):
                self.client.channel.send.return_value = {'call': candidate}
                with self.assertRaisesRegex(DeviceError, 'invalid_agent_tool_call'):
                    await self.client.poll_agent_tool()

    async def test_oversized_or_non_json_result_never_sent(self):
        for invalid in ({'name': '中' * 134}, {'name': float('nan')}, ['not an object'], {1: 'nonstring'}):
            with self.assertRaisesRegex(ValueError, 'invalid_agent_tool_result'):
                await self.client.send_agent_tool_result(7, 42, True, invalid)
        self.client.channel.send.assert_not_awaited()

    async def test_largest_result_fits_p1_envelope(self):
        epoch = 'f' * 32
        transport = Mock(wireless=True, write=AsyncMock(), readline=AsyncMock(return_value=b'P1,' + json.dumps({
            'epoch': epoch, 'seq': 0xffffffff, 'boot': 1, 'ok': True,
        }).encode() + b'\n'))
        self.client.link = transport
        self.client.channel = channel = Channel(transport, expected_mac=None)
        channel.epoch, channel.seq, channel.boot, channel.online = epoch, 0xfffffffe, 1, True
        channel.capabilities = CAPABILITIES.copy()
        result = {'name': 'a' * 789}
        self.assertEqual(len(json.dumps(result, separators=(',', ':'))), 800)
        await self.client.send_agent_tool_result(0xffffffff, 0xffffffff, True, result)
        transport.write.assert_awaited_once()
        request = transport.write.await_args.args[0]
        self.assertLessEqual(len(request), 1024)
        self.assertEqual(json.loads(request[3:])['result'], result)
