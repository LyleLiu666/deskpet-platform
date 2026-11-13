import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
import urllib.request
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from deskpet_platform.card_upload import BACKGROUND_RECEIPT_GRACE, MAX_FILE_BYTES, transfer_timeout, upload_file
from deskpet_platform.channel import AckTimeout, Channel, CommandRejected
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession, CardFile
from deskpet_platform.pairing import DeviceConfig


class CardUploadTests(unittest.IsolatedAsyncioTestCase):
    def test_background_deadline_accounts_for_large_slow_writes(self):
        for size, deadline in ((1024**2, 300), (8 * 1024**2, 1084), (MAX_FILE_BYTES, 16444)):
            with self.subTest(size=size):
                self.assertEqual(transfer_timeout(size, background=True), deadline)
        self.assertEqual(transfer_timeout(MAX_FILE_BYTES, background=True) + BACKGROUND_RECEIPT_GRACE, 16504)

    async def test_expired_background_budget_keeps_original_relay_and_never_starts_another_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"synthetic data")
            for outcome in ("stored", "http_body", "unconfirmed", "unclaimed", "unacknowledged"):
                with self.subTest(outcome=outcome):
                    config = DeviceConfig("02:00:00:00:00:01", bytes(32))
                    original = Client(config)
                    original.link.peer_address = "127.0.0.1"
                    original.channel.capabilities = {
                        "card_transfer": 1, "card_fetch_async": 1, "card_fetch_receipt": 1,
                    }
                    original.channel.online = True
                    session = BoardSession(Path(directory) / "device.json")
                    session.client = original
                    relay = SimpleNamespace(url="http://127.0.0.1/fixture", served=outcome != "unclaimed",
                                            close=lambda: None)
                    waiting = asyncio.Event()
                    finish = asyncio.Event()
                    cancelled = asyncio.Event()

                    async def send(kind, **_fields):
                        if kind == "card_fetch" and outcome != "unacknowledged":
                            return {"transfer_status": "pending"}
                        waiting.set()
                        try:
                            await finish.wait()
                        except asyncio.CancelledError:
                            cancelled.set()
                            # Real Channel invalidates the connection when a
                            # request is interrupted by cancellation.
                            original.channel.online = False
                            raise
                        if outcome == "http_body":
                            raise CommandRejected("card_transfer_failed", "http_body")
                        return {"stored": True}

                    original.channel.send = AsyncMock(side_effect=send)
                    real_wait = asyncio.wait
                    waits = []

                    async def cross_deadline(tasks, *, timeout):
                        [task] = tasks
                        await asyncio.wait_for(waiting.wait(), 1)
                        waits.append(timeout)
                        self.assertFalse(relay.close.called or task.cancelled())
                        if len(waits) == 1:
                            return set(), tasks
                        self.assertEqual(len(waits), 2)
                        self.assertTrue(original.channel.online)
                        if outcome in ("stored", "http_body"):
                            finish.set()
                            return await real_wait(tasks)
                        return set(), tasks

                    relay.close = Mock()
                    original.close = AsyncMock()
                    clock = Mock(side_effect=(0, 0, 0, .5, .5))
                    upload_asyncio = Mock(wraps=asyncio, wait=AsyncMock(side_effect=cross_deadline), sleep=AsyncMock())
                    upload_asyncio.get_running_loop.return_value = SimpleNamespace(time=clock)
                    with patch("deskpet_platform.card_upload.CardRelay") as relay_factory, patch(
                        "deskpet_platform.card_upload.asyncio", upload_asyncio
                    ), patch.object(
                        session, "open_wifi", AsyncMock()
                    ) as reconnect, patch("deskpet_platform.card_upload.transfer_timeout", return_value=0.5), patch(
                        "deskpet_platform.card_upload.BACKGROUND_RECEIPT_GRACE", 2
                    ):
                        # The clock crosses the nominal deadline in the fake
                        # wait. The running task still has the same relay.
                        relay_factory.return_value.start.return_value = relay
                        result = await session.upload_files([CardFile(source=str(source), path="desk/data.bin")])
                        relay_factory.assert_called_once()
                    relay.close.assert_called_once()
                    reconnect.assert_not_called()
                    self.assertEqual(waits, [0.5] if outcome in ("unclaimed", "unacknowledged") else [0.5, 2])
                    calls = original.channel.send.await_args_list
                    self.assertEqual(calls[0].args, ("card_fetch",))
                    if outcome == "unacknowledged":
                        self.assertEqual(len(calls), 1)
                    else:
                        self.assertEqual(len(calls), 2)
                        self.assertEqual(calls[1].args, ("card_fetch_status",))
                        self.assertEqual(calls[1].kwargs, {"transfer_id": calls[0].kwargs["transfer_id"]})
                    if outcome == "stored":
                        self.assertEqual(result["status"], "completed")
                    else:
                        self.assertEqual(result["status"], "failed")
                        reason = "device_error: card_transfer_failed (http_body)" if outcome == "http_body" \
                            else "card_transfer_unconfirmed"
                        self.assertEqual(result["failed"]["reason"], reason)
                    self.assertEqual(cancelled.is_set(), outcome not in ("stored", "http_body"))

    async def test_board_side_handshake_expiration_reconnects_without_replaying_an_accepted_transfer(self):
        mac = "02:00:00:00:00:01"

        class Wire:
            peer_address = "127.0.0.1"

            def __init__(self, reject):
                self.requests = []
                self.reject = reject

            async def write(self, raw):
                self.request = json.loads(raw[3:])
                if self.request["type"] != "hello":
                    self.requests.append(self.request)

            async def readline(self):
                request = self.request
                reply = {"epoch": request["epoch"], "seq": request["seq"], "boot": 1, "ok": True}
                if request["type"] == "hello":
                    reply.update(mac=mac, protocol=1, rx_bytes=2048)
                elif request["type"] == self.reject:
                    reply.update(ok=False, error="handshake_required")
                elif request["type"] == "card_fetch":
                    reply["transfer_status"] = "pending"
                elif request["type"] == "card_fetch_status":
                    reply["stored"] = True
                else:
                    raise AssertionError(request)
                return b"P1," + json.dumps(reply).encode() + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"synthetic file")
            for rejected in ("card_fetch", "card_fetch_status"):
                with self.subTest(rejected=rejected):
                    old_wire, new_wire = Wire(rejected), Wire(None)
                    original, recovered = Channel(old_wire, mac), Channel(new_wire, mac)
                    await original.handshake()
                    await recovered.handshake()
                    relay = SimpleNamespace(url="http://127.0.0.1/fixture", served=True, close=lambda: None)
                    resume = AsyncMock(return_value=recovered)

                    with patch("deskpet_platform.card_upload.CardRelay") as relay_factory, patch(
                        "deskpet_platform.card_upload.asyncio.sleep", AsyncMock()
                    ):
                        relay_factory.return_value.start.return_value = relay
                        result = await upload_file(original, "127.0.0.1", bytes(32), "desk/data.bin", source,
                                                   background=True, resume=resume)
                        relay_factory.assert_called_once()
                    resume.assert_awaited_once_with(original)
                    self.assertFalse(original.online)
                    self.assertEqual(result["bytes"], source.stat().st_size)
                    self.assertEqual([request["type"] for request in new_wire.requests],
                                     ["card_fetch", "card_fetch_status"] if rejected == "card_fetch"
                                     else ["card_fetch_status"])
                    self.assertEqual({request["transfer_id"] for request in old_wire.requests + new_wire.requests},
                                     {old_wire.requests[0]["transfer_id"]})
                    if rejected == "card_fetch":
                        self.assertEqual(new_wire.requests[0]["url"], old_wire.requests[0]["url"])

    async def test_only_retryable_http_failures_restart_transfer_after_reconnect(self):
        class Relay:
            def __init__(self, device_ip, _encrypted):
                self.url = f"http://{device_ip}/fixture-{len(relays)}"
                self.served = True
                self.closed = False
                relays.append(self)

            def start(self):
                return self

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "data.bin"
            source.write_bytes(b"synthetic file")
            cases = (("card_transfer_failed", "http_open", True),
                     ("card_transfer_failed", "http_headers", True),
                     ("card_transfer_failed", "http_body", True),
                     ("card_transfer_failed", "http_tag", True),
                     ("card_transfer_failed", "card_verify", False),
                     ("card_transfer_failed", "http_status", False),
                     ("unknown_card_transfer", None, False))
            for code, stage, retry in cases:
                with self.subTest(code=code, stage=stage):
                    relays = []
                    error = CommandRejected(code, stage)
                    original = SimpleNamespace(online=True)

                    async def accept_then_disconnect(kind, **_fields):
                        if kind == "card_fetch":
                            return {"transfer_status": "pending"}
                        self.assertEqual(kind, "card_fetch_status")
                        original.online = False
                        raise AckTimeout("lost control connection after accepted transfer")

                    original.send = AsyncMock(side_effect=accept_then_disconnect)
                    recovered = SimpleNamespace(online=True, transport=SimpleNamespace(peer_address="127.0.0.2"),
                                                send=AsyncMock(side_effect=[
                                                    {"transfer_status": "pending"}, error,
                                                    {"transfer_status": "pending"}, {"stored": True},
                                                ]))

                    async def reconnect(channel):
                        self.assertIs(channel, original)
                        self.assertFalse(relays[0].closed)
                        return recovered

                    async def send_file():
                        return await upload_file(original, "127.0.0.1", bytes(32), "desk/data.bin", source,
                                                 background=True, resume=AsyncMock(side_effect=reconnect))

                    with patch("deskpet_platform.card_upload.CardRelay", Relay), patch(
                        "deskpet_platform.card_upload.asyncio.sleep", AsyncMock()
                    ):
                        if retry:
                            result = await send_file()
                            self.assertEqual(result["bytes"], source.stat().st_size)
                        else:
                            with self.assertRaises(CommandRejected) as rejected:
                                await send_file()
                            self.assertIs(rejected.exception, error)

                    initial_start, initial_status = original.send.await_args_list
                    transfer_id = initial_start.kwargs["transfer_id"]
                    first_poll, terminal_poll, *replacement = recovered.send.await_args_list
                    for poll in (initial_status, first_poll, terminal_poll):
                        self.assertEqual(poll.args, ("card_fetch_status",))
                        self.assertEqual(poll.kwargs, {"transfer_id": transfer_id})
                    self.assertEqual(len(relays), 2 if retry else 1)
                    self.assertTrue(all(relay.closed for relay in relays))
                    if retry:
                        fresh_start, fresh_poll = replacement
                        self.assertEqual(fresh_start.args, ("card_fetch",))
                        self.assertNotEqual(fresh_start.kwargs["transfer_id"], transfer_id)
                        self.assertNotEqual(fresh_start.kwargs["url"], initial_start.kwargs["url"])
                        self.assertEqual(fresh_poll.args, ("card_fetch_status",))
                        self.assertEqual(fresh_poll.kwargs, {"transfer_id": fresh_start.kwargs["transfer_id"]})
                    else:
                        self.assertEqual(replacement, [])

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
