import base64
from pathlib import Path
import tempfile
import unittest

from deskpet_platform.usb_setup import load_network, probe_usb


class FakeSerial:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def open(self):
        assert self.port == "/dev/cu.synthetic"
        assert self.dtr is self.rts is False

    def write(self, value):
        self.requests.append(value)

    def read_until(self, *, size):
        return next(self.responses)


class UsbSetupTests(unittest.TestCase):
    def test_busy_board_returns_an_actionable_error_without_retrying(self):
        board = FakeSerial([b"DESK 02:00:00:00:00:01 0.0.0.0 setup\n", b"DESK BUSY\n"])
        with self.assertRaisesRegex(ValueError, "正在被控制"):
            probe_usb("/dev/cu.synthetic", "02:00:00:00:00:01", serial_factory=lambda **_: board)
        self.assertEqual(board.requests, [b"DESK?\n", b"DESK PAIR 02:00:00:00:00:01\n"])

    def test_restored_network_is_available_for_pairing_but_is_not_trial_success(self):
        identity = "02:00:00:00:00:01"
        key = bytes(range(32))
        code = b"D1-020000000001-" + base64.urlsafe_b64encode(key).rstrip(b"=")
        restored = b"DESK 02:00:00:00:00:01 192.168.1.2 restored\n"
        with tempfile.TemporaryDirectory() as directory:
            credentials = Path(directory) / "network.json"
            credentials.write_text('{"ssid":"synthetic","password":"test-only"}')
            credentials.chmod(0o600)
            board = FakeSerial([restored, b"DESK ACCEPTED\n", restored])
            with self.assertRaisesRegex(ValueError, "恢复"):
                probe_usb("/dev/cu.synthetic", identity, credentials, serial_factory=lambda **_: board)
            self.assertEqual(board.requests[-1], b"DESK?\n")
        board = FakeSerial([restored, b"DESK CODE " + code + b"\n"])
        paired = probe_usb("/dev/cu.synthetic", identity, serial_factory=lambda **_: board)
        self.assertEqual((paired.address, paired.key), ("192.168.1.2", key))

    def test_wifi_and_pairing_use_usb_and_verify_identity_before_writing_credentials(self):
        identity = "02:00:00:00:00:01"
        key = bytes(range(32))
        code = b"D1-020000000001-" + base64.urlsafe_b64encode(key).rstrip(b"=")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "network.json"
            config.write_text('{"ssid":"synthetic","password":"test-only"}')
            config.chmod(0o600)
            other = FakeSerial([b"DESK 02:00:00:00:00:02 0.0.0.0 connecting\n"])
            with self.assertRaisesRegex(ValueError, "身份"):
                probe_usb("/dev/cu.synthetic", identity, config, serial_factory=lambda **_: other)
            self.assertEqual(other.requests, [b"DESK?\n"])

            board = FakeSerial([
                b"DESK CONTROL ENDED\n",
                b"DESK 02:00:00:",
                b"00:00:01 0.0.0.0 connecting\n",
                b"DESK 02:00:00:00:00:01 0.0.0.0 connecting\n",
                b"DESK ACCEPTED\n",
                b"DESK 02:00:00:00:00:01 192.168.1.2 online\n",
                b"DESK CODE " + code + b"\n",
            ])
            paired = probe_usb("/dev/cu.synthetic", identity, config, serial_factory=lambda **_: board)
            self.assertEqual((paired.identity, paired.address, paired.key), (identity, "192.168.1.2", key))
            self.assertEqual(board.requests[1], b"DESK WIFI 02:00:00:00:00:01 " +
                             b"synthetic".hex().encode() + b" " + b"test-only".hex().encode() + b"\n")
            self.assertEqual(board.requests[-1], b"DESK PAIR 02:00:00:00:00:01\n")

    def test_network_credentials_must_be_private_and_regular(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "network.json"
            config.write_text('{"ssid":"synthetic","password":""}')
            config.chmod(0o644)
            with self.assertRaises(ValueError):
                load_network(config)
            config.chmod(0o600)
            link = config.with_name("link.json")
            link.symlink_to(config)
            with self.assertRaises(ValueError):
                load_network(link)
            self.assertEqual(load_network(config), (b"synthetic", b""))
