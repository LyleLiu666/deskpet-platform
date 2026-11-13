import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error
import urllib.request
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from deskpet_platform.card_upload import MAX_FILE_BYTES, upload_file
from deskpet_platform.channel import AckTimeout, CommandRejected
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession, CardFile
from deskpet_platform.pairing import DeviceConfig


class CardUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_start_receipt_keeps_relay_open_and_queries_original_job(self):
        class Relay:
            url, served, closed = "http://127.0.0.1/fixture", True, False
            def __init__(self, *_args): pass
            def start(self): return self
            def close(self): self.closed = True

        relay = Relay()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"synthetic file")
            config = DeviceConfig("02:00:00:00:00:01", bytes(32))
            original = Client(config)
            recovered = Client(config)
            original.device_id = recovered.device_id = "test-board"
            original.link.peer_address = "127.0.0.1"
            for client in (original, recovered):
                client.channel.capabilities = {"card_transfer": 1, "card_fetch_async": 1, "card_fetch_receipt": 1}
                client.channel.online = True
            original.close = AsyncMock()

            async def lose_receipt(kind, **fields):
                self.assertEqual(kind, "card_fetch")
                self.assertRegex(fields["transfer_id"], "^[0-9a-f]{32}$")
                recovered.channel.send = AsyncMock(side_effect=[
                    {"transfer_status": "pending"}, {"stored": True},
                    {"transfer_status": "pending"}, {"stored": True},
                ])
                original.channel.online = False
                raise AckTimeout("lost start receipt")

            original.channel.send = AsyncMock(side_effect=lose_receipt)
            session = BoardSession(Path(directory) / "device.json")
            session.client = original

            async def reconnect(config):
                self.assertFalse(relay.closed)
                self.assertEqual(config, original.config)
                return recovered

            with patch.object(session, "open_wifi", reconnect), patch(
                "deskpet_platform.card_upload.CardRelay", return_value=relay
            ), patch("deskpet_platform.card_upload.asyncio.sleep", AsyncMock()):
                result = await session.upload_files([
                    CardFile(source=str(source), path="desk/data.bin"),
                    CardFile(source=str(source), path="desk/next.bin"),
                ])
            self.assertEqual(result["status"], "completed")
            self.assertEqual([item["path"] for item in result["completed"]], ["desk/data.bin", "desk/next.bin"])
            self.assertIs(session.client, recovered)
            self.assertTrue(relay.closed)
            original.channel.send.assert_awaited_once()
            transfer_id = original.channel.send.await_args.kwargs["transfer_id"]
            self.assertEqual(recovered.channel.send.await_count, 4)
            for status in recovered.channel.send.await_args_list[:2]:
                self.assertEqual(status.args, ("card_fetch_status",))
                self.assertEqual(status.kwargs, {"transfer_id": transfer_id})
            next_start, next_status = recovered.channel.send.await_args_list[2:]
            self.assertEqual(next_start.args, ("card_fetch",))
            self.assertNotEqual(next_start.kwargs["transfer_id"], transfer_id)
            self.assertEqual(next_status.kwargs, {"transfer_id": next_start.kwargs["transfer_id"]})

    async def test_photo_upload_reconnects_and_waits_for_the_previous_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "screen.565"
            source.write_bytes(b"synthetic photo")
            path = "photos/p-0123456789abcdef/screen.565"
            config = DeviceConfig("02:00:00:00:00:01", bytes(32))
            channel = SimpleNamespace(online=True, capabilities={"card_fetch_async": 1})

            async def disconnect(*_args):
                channel.online = False
                raise OSError("wifi_disconnected")

            original = SimpleNamespace(config=config, device_id="test-board", link=SimpleNamespace(wireless=True),
                                       channel=channel, close=AsyncMock(), upload_file=AsyncMock(side_effect=disconnect))
            receipt = {"path": path, "bytes": 15}
            recovered = SimpleNamespace(config=config, device_id=original.device_id, link=original.link,
                                        channel=SimpleNamespace(online=True, capabilities={"card_fetch_async": 1}),
                                        upload_file=AsyncMock(side_effect=[CommandRejected("card_busy"), receipt]))
            session = BoardSession(root / "device.json")
            session.client = original
            with patch.object(session, "open_wifi", AsyncMock(return_value=recovered)), patch(
                "deskpet_platform.mcp_server.asyncio.sleep", AsyncMock()
            ):
                result = await session.upload_files([CardFile(source=str(source), path=path)])
            self.assertEqual(result, {"status": "completed", "completed": [receipt]})
            self.assertIs(session.client, recovered)
            original.close.assert_awaited_once()
            self.assertEqual(recovered.upload_file.await_count, 2)

    async def test_background_transfer_limits_status_polling(self):
        class Relay:
            url, served = "http://127.0.0.1/fixture", True
            def __init__(self, *_args): pass
            def start(self): return self
            def close(self): pass

        class Board:
            polls = 0
            async def send(self, kind, **_fields):
                if kind == "card_fetch":
                    return {"seq": 7, "transfer_status": "pending"}
                self.assert_kind(kind)
                self.polls += 1
                return {"stored": True} if self.polls == 3 else {"transfer_status": "pending"}
            def assert_kind(self, kind):
                if kind != "card_fetch_status":
                    raise AssertionError(kind)

        delays = []
        async def sleep(seconds): delays.append(seconds)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"synthetic data")
            with patch("deskpet_platform.card_upload.CardRelay", Relay), patch("deskpet_platform.card_upload.asyncio.sleep", sleep):
                result = await upload_file(Board(), "127.0.0.1", bytes(32), "desk/data.bin", source, background=True)
        self.assertFalse(result["unchanged"])
        self.assertEqual(len(delays), 3)
        self.assertTrue(all(delay >= 1 for delay in delays))

    async def test_touch_is_acknowledged_and_delivered_during_background_transfer(self):
        started, finish = asyncio.Event(), asyncio.Event()
        config = DeviceConfig("02:00:00:00:00:01", bytes(range(32)))

        class Wire:
            peer_address = "127.0.0.1"

            def __init__(self, *_args, **_kwargs):
                self.replies = asyncio.Queue()
                self.tap_sent = self.tap_acknowledged = False
                self.fetch = None

            def receive(self, message):
                self.replies.put_nowait(b"P1," + json.dumps(message).encode() + b"\n")

            async def write(self, raw):
                request = json.loads(raw[3:])
                reply = {"epoch": request["epoch"], "seq": request["seq"], "boot": 1, "ok": True}
                match request["type"]:
                    case "hello":
                        reply.update(mac=config.identity, protocol=1, rx_bytes=2048,
                                     card_transfer=1, card_fetch_async=1, touch_demo=1)
                    case "card_fetch":
                        self.fetch = request
                        started.set()
                        if request.get("background"):
                            reply["transfer_status"] = "pending"
                        else:
                            await finish.wait()
                            reply["stored"] = True
                    case "card_fetch_status":
                        assert request["operation"] == self.fetch["seq"]
                        if finish.is_set():
                            def fetch():
                                with urllib.request.urlopen(self.fetch["url"]) as response:
                                    return response.read()
                            ciphertext = await asyncio.to_thread(fetch)
                            assert len(ciphertext) == self.fetch["bytes"] + 16
                            reply["stored"] = True
                        else:
                            reply["transfer_status"] = "pending"
                    case "heartbeat":
                        if started.is_set() and not self.tap_sent:
                            self.receive({"type": "touch", "epoch": request["epoch"], "boot": 1, "operation": 4,
                                          "version": 0, "id": "screen", "action": "tap"})
                            self.tap_sent = True
                    case "touch_ack":
                        assert request["operation"] == 4
                        self.tap_acknowledged = True
                self.receive(reply)

            async def readline(self):
                return await self.replies.get()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frames.bin"
            source.write_bytes(b"synthetic frames")
            client = Client(config, link_factory=Wire)
            await client.channel.handshake()
            session = BoardSession(Path(directory) / "device.json")
            session.client = client
            upload = asyncio.create_task(session.upload_files(
                [CardFile(source=str(source), path="desk/frames.bin")]))
            poll = asyncio.create_task(session.poll())
            try:
                await asyncio.wait_for(started.wait(), 2)
                result = await session.wait_touch(2)
                self.assertEqual(result["status"], "received")
                self.assertTrue(client.link.tap_acknowledged)
                self.assertFalse(upload.done())
                finish.set()
                self.assertEqual((await asyncio.wait_for(upload, 2))["status"], "completed")
            finally:
                poll.cancel()
                upload.cancel()
                await asyncio.gather(poll, upload, return_exceptions=True)

    async def test_synthetic_board_receives_authenticated_encrypted_file(self):
        key = bytes(range(32))
        data = bytes(range(256)) * 17
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "frames.bin"
            source.write_bytes(data)

            class Board:
                async def send(self, kind, **fields):
                    self.kind, self.fields = kind, fields
                    def fetch():
                        with urllib.request.urlopen(fields["url"]) as response:
                            return response.read()
                    ciphertext = await asyncio.to_thread(fetch)
                    derived = hmac.new(key, b"desk-card-v1/key", hashlib.sha256).digest()
                    aad = f'desk-card-v1\n{fields["path"]}\n{len(data)}\n{zlib.crc32(data):08x}'.encode()
                    plain = AESGCM(derived).decrypt(bytes.fromhex(fields["iv"]), ciphertext, aad)
                    assert plain == data
                    with self_assert_raises(urllib.error.HTTPError):
                        await asyncio.to_thread(fetch)
                    return {"stored": True}

            self_assert_raises = self.assertRaises
            board = Board()
            result = await upload_file(board, "127.0.0.1", key, "roles/test/v1/frames.bin", source)
            self.assertEqual(board.kind, "card_fetch")
            self.assertEqual(board.fields["timeout"], 300)
            self.assertEqual(result["bytes"], len(data))
            self.assertEqual(result["path"], "roles/test/v1/frames.bin")
            self.assertNotIn(data, repr(board.fields).encode())

    async def test_rejects_bad_paths_and_empty_files_before_contacting_board(self):
        class Board:
            async def send(self, *_args, **_kwargs):
                raise AssertionError("invalid input must not reach the board")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file"
            source.write_bytes(b"x")
            for path in ("../outside", "roles/../escape", "roles/a//b", "other/file",
                         "roles/a/file.upload", "roles/a/file.previous"):
                with self.assertRaises(ValueError):
                    await upload_file(Board(), "127.0.0.1", bytes(32), path, source)
            source.write_bytes(b"")
            with self.assertRaises(ValueError):
                await upload_file(Board(), "127.0.0.1", bytes(32), "roles/a/file", source)

    async def test_large_wifi_upload_keeps_enough_time_for_a_slow_spi_card(self):
        class Relay:
            url = "http://127.0.0.1/fixture"
            served = True

            def __init__(self, *_args):
                pass

            def start(self):
                return self

            def close(self):
                pass

        class Board:
            async def send(self, command, *, timeout, **fields):
                self.timeout = timeout
                assert command == "card_fetch" and fields["bytes"] == MAX_FILE_BYTES
                return {"stored": True}

        board = Board()
        with (patch("deskpet_platform.card_upload.file_info", return_value=(MAX_FILE_BYTES, 0)),
              patch("deskpet_platform.card_upload.encrypt_file", return_value="fixture"),
              patch("deskpet_platform.card_upload.CardRelay", Relay)):
            result = await upload_file(board, "127.0.0.1", bytes(32), "desk/file.bin", "synthetic.bin")
        self.assertEqual(result["bytes"], MAX_FILE_BYTES)
        self.assertGreaterEqual(board.timeout, 60 + MAX_FILE_BYTES / (48 * 1024))

    async def test_changed_role_file_does_not_reach_device(self):
        class Board:
            async def send(self, *_args, **_kwargs):
                raise AssertionError("changed input must not reach the board")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file"
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                await upload_file(Board(), "127.0.0.1", bytes(32), "roles/a/file", source,
                                  expected_sha256=hashlib.sha256(b"original").hexdigest())
