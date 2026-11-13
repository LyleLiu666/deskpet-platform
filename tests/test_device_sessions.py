import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.device_sessions import DeviceSessions
from deskpet_platform.mcp_server import BoardSession, create_server
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.usb_link import UsbIdentity

KEY = bytes(range(32))
A = DeviceConfig('02:00:00:00:00:01', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f10')
B = DeviceConfig('02:00:00:00:00:02', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f11')


class FakeSession:
    def __init__(self, path, **kwargs):
        self.path = path
        self.calls = []

    async def show_text(self, value):
        self.calls.append(value)
        return {'device': DeviceConfig.load(self.path).identity}


class DeviceSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_request_routes_to_the_chosen_device_without_global_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            registry = DeviceRegistry(path)
            registry.add(B)
            manager = DeviceSessions(registry, FakeSession(path), FakeSession)
            with self.assertRaises(ValueError):
                manager.resolve(None)
            first = manager.resolve(A.device_id)
            second = manager.resolve(B.device_id)
            self.assertIsNot(first, second)
            self.assertEqual(await first.show_text('A'), {'device': A.identity})
            self.assertEqual(await second.show_text('B'), {'device': B.identity})
            self.assertEqual(first.calls, ['A'])
            self.assertEqual(second.calls, ['B'])
            with self.assertRaises(KeyError):
                manager.resolve('01020304-0506-4708-890a-0b0c0d0e0f12')

    async def test_single_device_keeps_legacy_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            default = FakeSession(path)
            manager = DeviceSessions(DeviceRegistry(path), default, FakeSession)
            self.assertIs(manager.resolve(None), default)
            self.assertIs(manager.resolve(A.device_id), default)

    async def test_mcp_requires_an_explicit_id_and_routes_to_both_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            DeviceRegistry(path).add(B)
            with patch('deskpet_platform.mcp_server.BoardSession', FakeSession):
                server = create_server(path)
                tools = {tool.name: tool for tool in await server.list_tools()}
                self.assertIn('device_id', tools['desk_show_text'].inputSchema['properties'])
                listed = await server.call_tool('desk_list_devices', {})
                self.assertEqual({item['id'] for item in listed[1]['devices']}, {A.device_id, B.device_id})
                with self.assertRaises(Exception):
                    await server.call_tool('desk_show_text', {'text': 'missing selection'})
                self.assertEqual((await server.call_tool('desk_show_text', {'text': 'A', 'device_id': A.device_id}))[1]['device'], A.identity)
                self.assertEqual((await server.call_tool('desk_show_text', {'text': 'B', 'device_id': B.device_id}))[1]['device'], B.identity)
                with self.assertRaises(Exception):
                    await server.call_tool('desk_show_text', {'text': 'unknown', 'device_id': '01020304-0506-4708-890a-0b0c0d0e0f12'})

    async def test_mcp_finds_the_current_port_by_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(Path(directory) / "device.json")
            paths = ["/dev/cu.first", "/dev/cu.second"]
            with patch("deskpet_platform.usb_link.read_usb_identity") as read:
                read.side_effect = [UsbIdentity(B.identity, B.device_id), UsbIdentity(A.identity, A.device_id)]
                result = await server.call_tool("desk_find_usb_device", {"ports": paths, "device_id": A.device_id})
            self.assertEqual(result[1], {"devices": [{"port": paths[1], "identity": A.identity,
                                                     "device_id": A.device_id}], "unavailable": []})
            self.assertEqual([call.args[0] for call in read.call_args_list], paths)

    async def test_adding_usb_device_authenticates_before_storing_without_replacing_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            original = path.read_bytes()
            board = AsyncMock()
            board.__aenter__.return_value = board
            board.device_id = B.device_id
            port = MagicMock()
            with patch('deskpet_platform.mcp_server.BoardSession', FakeSession), \
                 patch.object(FakeSession, 'usb_client', return_value=board, create=True), \
                 patch('deskpet_platform.mcp_server.UsbPort.register', return_value=port), \
                 patch('deskpet_platform.mcp_server.probe_usb', return_value=DeviceConfig(B.identity, KEY)):
                server = create_server(path)
                result = await server.call_tool('desk_pair_usb', {
                    'port': '/dev/synthetic', 'expected_identity': B.identity, 'add_device': True,
                })
            self.assertEqual(result[1]['id'], B.device_id)
            board.status.assert_awaited_once()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(DeviceRegistry(path).get(B.device_id), B)

    async def test_failed_usb_authentication_does_not_register_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            board = AsyncMock()
            board.__aenter__.side_effect = OSError('authentication failed')
            with patch('deskpet_platform.mcp_server.BoardSession', FakeSession), \
                 patch.object(FakeSession, 'usb_client', return_value=board, create=True), \
                 patch('deskpet_platform.mcp_server.UsbPort.register'), \
                 patch('deskpet_platform.mcp_server.probe_usb', return_value=DeviceConfig(B.identity, KEY)):
                server = create_server(path)
                with self.assertRaises(Exception):
                    await server.call_tool('desk_pair_usb', {
                        'port': '/dev/synthetic', 'expected_identity': B.identity, 'add_device': True,
                    })
            self.assertEqual([item['id'] for item in DeviceRegistry(path).list()], [A.device_id])

    async def test_usb_network_setup_uses_the_selected_existing_device(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            registry = DeviceRegistry(path)
            registry.add(B)
            with patch.object(BoardSession, 'pair_usb', autospec=True) as pair_usb, \
                 patch('deskpet_platform.mcp_server.probe_usb') as probe:
                pair_usb.return_value = {'device': B.identity, 'device_id': B.device_id}
                server = create_server(path)
                arguments = {'port': '/dev/synthetic', 'expected_identity': B.identity}
                with self.assertRaises(Exception):
                    await server.call_tool('desk_pair_usb', arguments)
                with self.assertRaises(Exception):
                    await server.call_tool('desk_pair_usb', {**arguments, 'add_device': True})
                result = await server.call_tool('desk_pair_usb', {**arguments, 'device_id': B.device_id})
                self.assertEqual(result[1]['device_id'], B.device_id)
                pair_usb.assert_awaited_once()
                target, *arguments = pair_usb.await_args.args
                self.assertEqual(target.path, registry.path_for(B.device_id))
                self.assertEqual(arguments, ['/dev/synthetic', B.identity, None])
                probe.assert_not_called()

    async def test_authenticated_legacy_session_upgrades_its_saved_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            legacy = DeviceConfig(A.identity, KEY)
            legacy.save(path)
            session = BoardSession(path)
            session.identity_registry = DeviceRegistry(path)
            board = AsyncMock()
            board.device_id = A.device_id
            board.config = legacy
            board.channel.capabilities = {}
            board.status.return_value = {}
            session.connect = AsyncMock(return_value=board)
            session.recover_wifi = AsyncMock()
            session.record_status = MagicMock()
            with patch('deskpet_platform.mcp_server.deliver_next', AsyncMock()), \
                 patch('deskpet_platform.mcp_server.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
            self.assertEqual(DeviceConfig.load(path), A)
            self.assertEqual(board.config, A)

    async def test_pairing_a_different_device_cannot_overwrite_existing_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            session = BoardSession(path)
            session._verify_pairing = AsyncMock(return_value=B.device_id)
            code = 'D1-' + B.identity.replace(':', '') + '-' + base64.urlsafe_b64encode(KEY).decode().rstrip('=')
            with self.assertRaises(ValueError):
                await session.pair(code, None)
            session._verify_pairing.assert_not_awaited()
            self.assertEqual(DeviceConfig.load(path), A)

    async def test_repairing_an_existing_uuid_rejects_a_different_or_missing_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            session = BoardSession(path)
            code = 'D1-' + A.identity.replace(':', '') + '-' + base64.urlsafe_b64encode(KEY).decode().rstrip('=')
            for value in (B.device_id, None):
                session._verify_pairing = AsyncMock(return_value=value)
                with self.assertRaises(ValueError):
                    await session.pair(code, None)
                self.assertEqual(DeviceConfig.load(path), A)
