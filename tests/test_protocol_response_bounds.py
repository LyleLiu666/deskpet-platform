import json
import unittest

from deskpet_platform.channel import Channel, DeviceError
from deskpet_platform.wifi_secure import MAX_PLAINTEXT

MAC = '02:00:00:00:00:01'


class Wire:
    def __init__(self, size):
        self.size = size

    async def write(self, data):
        request = json.loads(data[3:])
        response = {'epoch': request['epoch'], 'seq': request['seq'], 'boot': 1, 'ok': True,
                    'mac': MAC, 'protocol': 1, 'rx_bytes': 2048, 'padding': ''}
        base = b'P1,' + json.dumps(response, separators=(',', ':')).encode() + b'\n'
        response['padding'] = 'x' * (self.size - len(base))
        self.response = b'P1,' + json.dumps(response, separators=(',', ':')).encode() + b'\n'
        assert len(self.response) == self.size

    async def readline(self):
        return self.response

    async def close(self):
        pass


class ResponseBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_accepts_a_complete_authenticated_record_up_to_transport_limit(self):
        for size in (1024, 1110, MAX_PLAINTEXT):
            with self.subTest(size=size):
                channel = Channel(Wire(size), MAC)
                self.assertEqual((await channel.handshake())['mac'], MAC)

    async def test_oversized_response_is_rejected(self):
        channel = Channel(Wire(MAX_PLAINTEXT + 1), MAC)
        with self.assertRaises(DeviceError):
            await channel.handshake()


class ExtendedWire:
    def __init__(self, extension, *, advertised=True, extension_boot=4):
        self.extension = extension
        self.advertised = advertised
        self.extension_boot = extension_boot
        self.commands = []

    async def write(self, line):
        request = json.loads(line[3:])
        command = request['type']
        self.commands.append(command)
        response = dict(epoch=request['epoch'], seq=request['seq'], boot=4, ok=True)
        if command == 'hello':
            response.update(type='capabilities', mac=MAC, protocol=1, rx_bytes=2048,
                            dashboard_rotation=1)
            if self.advertised:
                response['cap_ext'] = 1
        else:
            assert command == 'capabilities_ext'
            response.update(boot=self.extension_boot)
            response.update(self.extension)
        self.response = b'P1,' + json.dumps(response).encode() + b'\n'

    async def readline(self):
        return self.response


class CapabilityExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_handshake_finishes_without_second_request(self):
        wire = ExtendedWire({}, advertised=False)
        channel = Channel(wire, MAC)
        self.assertEqual((await channel.handshake())['dashboard_rotation'], 1)
        self.assertEqual(wire.commands, ['hello'])

    async def test_extension_is_merged_before_connection_becomes_online(self):
        wire = ExtendedWire({'dashboard_rotation': 1, 'dashboard_interval': 1})
        channel = Channel(wire, MAC)
        caps = await channel.handshake()
        self.assertEqual(wire.commands, ['hello', 'capabilities_ext'])
        self.assertEqual(caps['dashboard_interval'], 1)
        self.assertEqual(caps['seq'], 1)
        self.assertEqual(channel.capabilities, caps)
        self.assertTrue(channel.online)

    async def test_failed_extension_clears_previous_handshake(self):
        for extension, boot in (({'mac': MAC}, 4), ({'music_control': True}, 4),
                                ({'dashboard_rotation': 0}, 4), ({}, 5)):
            with self.subTest(extension=extension, boot=boot):
                wire = ExtendedWire(extension, advertised=False, extension_boot=boot)
                channel = Channel(wire, MAC)
                await channel.handshake()
                wire.advertised = True
                with self.assertRaises(DeviceError):
                    await channel.handshake()
                self.assertFalse(channel.online)
                self.assertEqual(channel.capabilities, {})
