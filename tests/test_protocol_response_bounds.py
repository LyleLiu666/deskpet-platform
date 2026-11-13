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
