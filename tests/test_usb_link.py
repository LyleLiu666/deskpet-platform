import asyncio
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform.card_upload import upload_usb
from deskpet_platform.usb_link import MacSerial, UsbLink, UsbPort, read_usb_identity

TEST_PORT = "COM5" if sys.platform == "win32" else "/dev/cu.synthetic"

class UsbLinkTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS serial ioctl")
    def test_macos_serial_open_releases_both_reset_lines_together(self):
        import termios
        device = MacSerial(port=None)
        device.fd = 7
        with patch("deskpet_platform.usb_link.fcntl.ioctl") as ioctl:
            device._update_dtr_state()
            device._update_rts_state()
        ioctl.assert_called_once_with(7, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))

    async def test_control_session_reads_split_protocol_lines_and_releases_device(self):
        class Board:
            def __init__(self):
                self.replies = iter((b"DESK CONTROL READY\r\n", b"P1,{\"ok\":", b"true}\r\n"))
                self.commands = []
                self.closed = False
            def open(self):
                pass
            def write(self, data):
                self.commands.append(data)
            def read_until(self, *, size):
                return next(self.replies)
            def flush(self):
                pass
            def close(self):
                self.closed = True

        board = Board()
        with tempfile.TemporaryDirectory() as directory:
            link = UsbLink("02:00:00:00:00:01", bytes(32), port=TEST_PORT, lock_root=directory,
                           serial_factory=lambda **_: board)
            await link.open()
            try:
                await link.write(b"P1,{}\n")
                self.assertEqual(await asyncio.wait_for(link.readline(), 1), b'P1,{"ok":true}\n')
            finally:
                await link.close()
            self.assertEqual(board.commands, [b"DESK CONTROL 02:00:00:00:00:01\n", b"P1,{}\n", b"DESK END\n"])
            self.assertTrue(board.closed)

    async def test_port_replacement_is_rejected_before_open(self):
        invalid = UsbPort(TEST_PORT, (-1, -1, -1))
        opened = []
        with tempfile.TemporaryDirectory() as directory:
            link = UsbLink("02:00:00:00:00:01", bytes(32), port=invalid, lock_root=directory,
                           serial_factory=lambda **_: opened.append(True))
            with patch.object(UsbPort, "_identity", return_value=(1,)):
                with self.assertRaisesRegex(OSError, "usb_port_changed"):
                    await link.open()
            self.assertEqual(opened, [])

    def test_identity_follows_the_board_after_port_reassignment(self):
        first = "02:00:00:00:00:01"
        second = "02:00:00:00:00:02"
        uuid_a = "01020304-0506-4708-890a-0b0c0d0e0f10"
        uuid_b = "01020304-0506-4708-890a-0b0c0d0e0f11"

        class Board:
            def __init__(self, mac, uuid):
                self.replies = iter((b"startup\r\n", f"DESK IDENTITY {mac} ".encode(), uuid.encode() + b"\r\n"))
                self.commands = []

            def open(self):
                assert self.dtr is self.rts is False

            def write(self, command):
                self.commands.append(command)

            def read_until(self, *, size):
                return next(self.replies)

            def close(self):
                self.closed = True

        with patch.object(UsbPort, "_identity", side_effect=[(1,), (1,), (1,), (1,), (2,), (2,), (2,), (2,)]):
            for mac, uuid in ((first, uuid_a), (second, uuid_b)):
                board = Board(mac, uuid)
                registration = UsbPort.register(TEST_PORT)
                observed = read_usb_identity(registration, serial_factory=lambda **_: board)
                self.assertEqual((observed.identity, observed.device_id), (mac, uuid))
                self.assertEqual(board.commands, [b"DESK IDENTITY?\n"])
                self.assertTrue(board.closed)

    def test_identity_retries_on_the_same_port_after_adapter_resets_board(self):
        elapsed = 0.0
        mac, uuid = "02:00:00:00:00:01", "01020304-0506-4708-890a-0b0c0d0e0f10"

        class StartingBoard:
            def __init__(self):
                self.probes = 0
                self.opened = 0

            def open(self):
                self.opened += 1

            def write(self, request):
                assert request == b"DESK IDENTITY?\n"
                self.probes += 1

            def read_until(self, *, size):
                nonlocal elapsed
                elapsed += 0.25
                return f"DESK IDENTITY {mac} {uuid}\n".encode() if self.probes > 1 else b""

            def close(self):
                pass

        board = StartingBoard()
        with patch.object(UsbPort, "_identity", return_value=(1,)), patch(
                "deskpet_platform.usb_link.time.monotonic", side_effect=lambda: elapsed):
            port = UsbPort.register(TEST_PORT)
            observed = read_usb_identity(port, serial_factory=lambda **_: board)
        self.assertEqual((observed.identity, observed.device_id), (mac, uuid))
        self.assertEqual((board.opened, board.probes), (1, 2))

    async def test_serial_upload_checks_every_offset_and_hash_before_publishing(self):
        class Channel:
            online = True

            def __init__(self, wrong_offset=False):
                self.commands = []
                self.received = bytearray()
                self.wrong_offset = wrong_offset

            async def send(self, name, **fields):
                self.commands.append(name)
                if name == "card_usb_chunk":
                    assert fields["offset"] == len(self.received)
                    self.received.extend(bytes.fromhex(fields["data"]))
                    return {"offset": len(self.received) - self.wrong_offset}
                return {"stored": name == "card_usb_commit"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture"
            data = bytes(range(256)) * 4
            path.write_bytes(data)
            channel = Channel()
            receipt = await upload_usb(channel, "photos/sample/full.565", path,
                                       expected_sha256=hashlib.sha256(data).hexdigest())
            self.assertEqual(receipt["bytes"], len(data))
            self.assertEqual(channel.received, data)
            self.assertEqual(channel.commands[-1], "card_usb_commit")
            wrong = Channel(wrong_offset=True)
            with self.assertRaises(RuntimeError):
                await upload_usb(wrong, "photos/sample/full.565", path)
            self.assertEqual(wrong.commands[-1], "card_usb_abort")

class WindowsUsbPortTests(unittest.TestCase):
    def test_com_port_names_are_explicit_and_cannot_target_other_devices(self):
        from deskpet_platform.usb_link import valid_usb_port
        for value in ("COM1", "com12", "COM4096"):
            self.assertTrue(valid_usb_port(value, windows=True), value)
        for value in ("COM0", "COM01", "COM4097", "COM1/foo", "LPT1", "\\\\.\\pipe\\other", "/dev/tty.fake"):
            self.assertFalse(valid_usb_port(value, windows=True), value)

    def test_windows_port_identity_changes_when_the_attached_adapter_changes(self):
        from deskpet_platform.usb_link import windows_port_identity
        from types import SimpleNamespace
        first = SimpleNamespace(device="COM5", vid=0x1a86, pid=0x7523, serial_number="fixture1", location="1-1", hwid="fixture")
        second = SimpleNamespace(device="COM5", vid=0x1a86, pid=0x7523, serial_number="fixture2", location="1-1", hwid="fixture")
        self.assertNotEqual(windows_port_identity("com5", [first]), windows_port_identity("COM5", [second]))
        with self.assertRaises(FileNotFoundError):
            windows_port_identity("COM6", [first])
        with self.assertRaises(ValueError):
            windows_port_identity("COM5", [first, second])
