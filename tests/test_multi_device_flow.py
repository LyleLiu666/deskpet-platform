"""Multi-device routing exercises the actual MCP tools and P1 channel.

The two boards and credentials are synthetic; no LAN device is contacted.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.client import Client
from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.mcp_server import BoardSession, create_server
from deskpet_platform.pairing import DeviceConfig

KEY = bytes(range(32))
FIRST = DeviceConfig('02:00:00:00:00:01', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f10')
SECOND = DeviceConfig('02:00:00:00:00:02', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f11')


class SyntheticBoard:
    def __init__(self, config):
        self.config = config
        self.brightness = []
        self.response = asyncio.Queue()
        self.computer_prefs = 0

    def link(self, identity, key, peer_hint=None):
        assert identity == self.config.identity and key == self.config.key
        return self

    async def open(self):
        pass

    async def close(self):
        pass

    async def write(self, data):
        request = json.loads(data[3:])
        response = {'epoch': request['epoch'], 'seq': request['seq'], 'boot': 1, 'ok': True}
        if request['type'] == 'hello':
            response.update(mac=self.config.identity, protocol=1, rx_bytes=2048,
                            device_id=self.config.device_id, brightness_control=1, compact_status=1,
                            computer_preferences=1, computer_metrics=1)
        elif request['type'] == 'heartbeat':
            response['computer_prefs'] = self.computer_prefs
        elif request['type'] == 'computer_preferences':
            assert not self.computer_prefs & 4
            self.computer_prefs = ((self.computer_prefs & ~3) |
                                   int(request['cpu_enabled']) | (int(request['memory_enabled']) << 1))
            response['computer_prefs'] = self.computer_prefs
        elif request['type'] == 'computer_prefs_ack':
            assert request['state'] == self.computer_prefs and self.computer_prefs & 4
            self.computer_prefs &= ~4
            response['computer_prefs'] = self.computer_prefs
        elif request['type'] == 'brightness_set':
            self.brightness.append(request['level'])
            response['brightness'] = request['level']
        else:
            raise AssertionError('unexpected command: ' + request['type'])
        await self.response.put(b'P1,' + json.dumps(response).encode() + b'\n')

    async def readline(self):
        return await self.response.get()


class MultiDeviceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_routes_concurrent_commands_over_separate_authenticated_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            FIRST.save(path)
            registry = DeviceRegistry(path)
            registry.add(SECOND)
            boards = {item.identity: SyntheticBoard(item) for item in (FIRST, SECOND)}
            instances = {}

            class TestSession(BoardSession):
                def __init__(self, path, **kwargs):
                    super().__init__(path, client_factory=lambda cfg: Client(cfg, link_factory=boards[cfg.identity].link), **kwargs)
                    instances[DeviceConfig.load(Path(path)).identity] = self

            with patch('deskpet_platform.mcp_server.BoardSession', TestSession):
                server = create_server(path)
                await server.call_tool('desk_status', {'device_id': SECOND.device_id})
                for config in (FIRST, SECOND):
                    instances[config.identity].client = await instances[config.identity].connect(config)
                try:
                    results = await asyncio.gather(
                        server.call_tool('desk_set_brightness', {'device_id': FIRST.device_id, 'level': 30}),
                        server.call_tool('desk_set_brightness', {'device_id': SECOND.device_id, 'level': 80}),
                    )
                    self.assertEqual([result[1]['brightness'] for result in results], [30, 80])
                    with self.assertRaises(Exception):
                        await server.call_tool('desk_set_brightness', {'level': 50})
                    with self.assertRaises(Exception):
                        await server.call_tool('desk_set_brightness', {'device_id': '01020304-0506-4708-890a-0b0c0d0e0f12', 'level': 50})
                    self.assertEqual(boards[FIRST.identity].brightness, [30])
                    self.assertEqual(boards[SECOND.identity].brightness, [80])
                    first = await server.call_tool('desk_computer_metrics', {'device_id': FIRST.device_id, 'cpu': True})
                    second = await server.call_tool('desk_computer_metrics', {'device_id': SECOND.device_id, 'memory': True})
                    self.assertEqual(first[1]['enabled'], {'cpu': True, 'memory': False})
                    self.assertEqual(second[1]['enabled'], {'cpu': False, 'memory': True})
                    self.assertEqual([boards[item.identity].computer_prefs for item in (FIRST, SECOND)], [1, 2])
                    first_session = instances[FIRST.identity]
                    await first_session.drop()
                    await server.call_tool('desk_computer_metrics', {'device_id': FIRST.device_id, 'cpu': False})
                    first_board = boards[FIRST.identity]
                    first_board.computer_prefs = 15  # Offline memory toggle on the first board.
                    first_session = TestSession(path)
                    first_session.client = await first_session.connect(FIRST)
                    await first_session.sync_computer_preferences(
                        first_session.client, await first_session.client.status(compact=True))
                    self.assertEqual(first_board.computer_prefs, 10)
                    self.assertEqual(boards[SECOND.identity].computer_prefs, 2)
                    with self.assertRaises(Exception):
                        await server.call_tool('desk_computer_metrics', {'cpu': True})
                finally:
                    for session in instances.values():
                        await session.drop()
