import asyncio
import hashlib
from pathlib import Path
import struct
import tempfile
import termios
import unittest
from unittest.mock import patch

from deskpet_platform.card_upload import upload_usb
from deskpet_platform.usb_link import MacSerial, UsbLink, UsbPort


class UsbLinkTests(unittest.IsolatedAsyncioTestCase):
    def test_macos_serial_open_releases_both_reset_lines_together(self):
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
            link = UsbLink("02:00:00:00:00:01", bytes(32), port="/dev/cu.synthetic", lock_root=directory,
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
        invalid = UsbPort("/dev/null", (-1, -1, -1))
        opened = []
        with tempfile.TemporaryDirectory() as directory:
            link = UsbLink("02:00:00:00:00:01", bytes(32), port=invalid, lock_root=directory,
                           serial_factory=lambda **_: opened.append(True))
            with self.assertRaisesRegex(OSError, "usb_port_changed"):
                await link.open()
            self.assertEqual(opened, [])

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
