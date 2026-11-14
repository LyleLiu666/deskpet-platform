import asyncio
import base64
import json
import hashlib
import hmac
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import zlib
import urllib.request
from zipfile import ZipFile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from deskpet_platform.channel import AckTimeout, CommandRejected, DeviceError
from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession, status_view
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.usb_link import UsbIdentity, UsbPort
from deskpet_platform.wifi_link import WifiLink, probe_board
from deskpet_platform.wifi_secure import RecordCodec, proof, session_key
from test_role_pack import make_pack, write_role_actions

DEVICE = "02:00:00:00:00:01"
KEY = bytes(range(32))


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_deletion_targets_a_complete_role_photo_or_one_desk_file(self):
        class Channel:
            capabilities = {"card_delete": 1}

            async def send(self, command, *, timeout, **fields):
                assert timeout == 30
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        for path in ("roles/my-role", "photos/p-0123456789abcdef", "desk/assets/icon.bin"):
            self.assertEqual(await client.delete_resource(path), ("card_delete", {"path": path}))
        for path in ("roles/my-role/active", "photos/p-0123456789abcdef/full.565", "desk/../file",
                     "desk/assets./icon.bin", "desk/assets/icon.bin."):
            with self.assertRaises(ValueError):
                await client.delete_resource(path)

    async def test_clock_edit_sdk_validates_a_single_field_adjustment(self):
        class Channel:
            capabilities = {"clock_edit": 1}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        self.assertEqual(await client.clock_edit("adjust", field="minute", step=-1),
                         ("clock_edit", {"mode": "adjust", "field": "minute", "step": -1}))
        self.assertEqual(await client.clock_edit("cancel"), ("clock_edit", {"mode": "cancel"}))
        for mode, fields in (("adjust", {}), ("adjust", {"field": "minute", "step": True}),
                             ("adjust", {"field": "second", "step": 1}), ("save", {"step": 1})):
            with self.assertRaises(ValueError):
                await client.clock_edit(mode, **fields)

    async def test_album_can_be_saved_as_the_startup_page(self):
        class Channel:
            capabilities = {"screen_views": 1}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        self.assertEqual(await client.set_view("photos", make_default=True),
                         ("screen_view", {"page": 5, "make_default": True}))
        with self.assertRaises(ValueError):
            await client.set_view("weather", make_default=True)

    async def test_message_list_uses_the_display_firmware_navigation_command(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", no_token_probe=True)
            self.assertEqual((await session.message_view("list", None))["status"], "not_connected")
            client = Client(DeviceConfig(DEVICE, KEY))
            client.channel = AsyncMock()
            client.channel.capabilities = {"agent_home": 1, "screen_views": 1}
            client.channel.send.return_value = {"screen_page": 7}
            session.client = client

            self.assertEqual(await session.message_view("list", None), {"status": "ok", "view": "list"})
            client.channel.send.assert_awaited_once_with("screen_view", page=7, make_default=False)
            for mode, identity in (("open", 2), ("previous", None), ("next", None), ("mark_seen", 2)):
                self.assertEqual((await session.message_view(mode, identity))["status"], "unsupported")
            with self.assertRaises(ValueError):
                await session.message_view("list", 2)
            client.channel.send.assert_awaited_once()

            client.channel.send.side_effect = CommandRejected("screen_busy")
            with self.assertRaisesRegex(CommandRejected, "screen_busy"):
                await session.message_view("list", None)

            client.channel.send.side_effect = CommandRejected("cache_not_ready")
            self.assertEqual((await session.message_view("list", None))["status"], "cache_not_ready")

            client.channel.send.reset_mock(side_effect=True)
            client.channel.send.return_value = {"screen_page": 0}
            with self.assertRaisesRegex(DeviceError, "message_list_not_confirmed"):
                await session.message_view("list", None)

            client.channel.send.reset_mock()
            client.channel.capabilities["message_view"] = 1
            client.channel.send.return_value = {"view": "list", "cached_count": 3}
            self.assertEqual(await session.message_view("list", None),
                             {"status": "ok", "view": "list", "cached_count": 3})
            client.channel.send.assert_awaited_once_with("message_view", mode="list")

    async def test_clock_sync_distinguishes_automatic_initialization_from_explicit_sync(self):
        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = AsyncMock()
        client.channel.capabilities = {"clock_sync": 1}

        await client.sync_clock(345, only_missing=True)
        await client.sync_clock(345)

        automatic, explicit = client.channel.send.await_args_list
        self.assertEqual(automatic.args, ("clock_set",))
        self.assertEqual(explicit.args, ("clock_set",))
        self.assertIs(automatic.kwargs["only_missing"], True)
        self.assertNotIn("only_missing", explicit.kwargs)
        self.assertEqual(automatic.kwargs["offset_minutes"], 345)
        self.assertEqual(explicit.kwargs["offset_minutes"], 345)

    async def test_speed_windows_round_trip_and_token_sample_metadata(self):
        class Channel:
            capabilities = {"speed_windows": 1, "token_stats": 1}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        windows = {"recent_seconds": 120, "long_days": 3}
        self.assertEqual(await client.speed_windows(), ("speed_windows", {}))
        self.assertEqual(await client.speed_windows(120, 3), ("speed_windows", {"speed_windows": windows}))
        for recent, days in ((120, None), (None, 3), (121, 3), (120, True)):
            with self.assertRaises(ValueError):
                await client.speed_windows(recent, days)
        snapshot = {"rate10": 80}
        self.assertEqual(await client.update_tokens(snapshot), ("token_update", snapshot))
        self.assertEqual(await client.update_tokens(snapshot, windows=windows),
                         ("token_update", {**snapshot, "speed_windows": windows}))

    async def test_wifi_probe_accepts_only_the_paired_board_proof(self):
        class Board(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, packet, source):
                prefix, identity, nonce = packet.decode().split(",")
                assert prefix == "DESK-PROBE-1" and identity == DEVICE and len(nonce) == 32
                payload = f"probe|desk-link-v1|{identity}|{nonce}|".encode()
                reply = hmac.digest(KEY, payload, "sha256")[:16].hex().encode()
                self.transport.sendto(b"DESK-PROBE-1," + reply, source)

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Board, local_addr=("127.0.0.1", 0))
        try:
            port = transport.get_extra_info("sockname")[1]
            self.assertTrue(await probe_board(DEVICE, KEY, "127.0.0.1", port=port))
            self.assertFalse(await probe_board(DEVICE, bytes(32), "127.0.0.1", port=port))
        finally:
            transport.close()

    async def test_usb_handoff_waits_for_active_work_and_keeps_usb_if_wifi_fails(self):
        config = DeviceConfig(DEVICE, KEY)
        ready = {"wifi_ip": "192.168.1.42", "wifi_handoff_ready": True}
        usb = AsyncMock()
        usb.config = config
        usb.channel.capabilities = {"wifi_probe": 1}
        usb.channel.dropped_events = 0
        usb.status.return_value = ready
        wifi = AsyncMock()
        wifi.__aenter__.side_effect = OSError("wifi_device_absent")
        restored = AsyncMock()
        restored.__aenter__.return_value = restored
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.probe_board", new_callable=AsyncMock, return_value=True,
        ) as probe:
            session = BoardSession(Path(directory) / "device.json", client_factory=lambda _: wifi)
            session.usb_port = object()
            session.usb_client = lambda *_: restored
            session.client = usb
            await session.recover_wifi(usb, {**ready, "wifi_handoff_ready": False})
            async with session.storage_lock:
                await session.recover_wifi(usb, ready)
            probe.assert_not_called()
            usb.close.assert_not_called()
            await session.recover_wifi(usb, ready)
            probe.assert_awaited_once_with(DEVICE, KEY, ready["wifi_ip"])
            usb.close.assert_awaited_once()
            self.assertIs(session.client, restored)

    async def test_connect_uses_usb_if_wifi_handshake_times_out(self):
        config = DeviceConfig(DEVICE, KEY, "192.168.1.42")
        wifi, usb = AsyncMock(), AsyncMock()
        wifi.__aenter__.side_effect = AckTimeout("fixture_hello_timeout")
        usb.__aenter__.return_value = usb
        path = "/dev/cu.synthetic"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            UsbPort, "_identity", return_value=(1,),
        ), patch("deskpet_platform.mcp_server.read_usb_identity") as read:
            session = BoardSession(Path(directory) / "device.json", client_factory=lambda _: wifi, usb_port=path)
            with patch.object(session, "usb_client", return_value=usb) as usb_client:
                self.assertIs(await session.connect(config), usb)
                usb_client.assert_called_once_with(config, UsbPort(path, (1,)))
                read.assert_not_called()

    async def test_usb_reconnect_revalidates_the_device_after_node_replacement(self):
        uuid = "01020304-0506-4708-890a-0b0c0d0e0f10"
        config = DeviceConfig(DEVICE, KEY, device_id=uuid)
        wifi, usb = AsyncMock(), AsyncMock()
        wifi.__aenter__.side_effect = OSError("wifi_absent")
        usb.__aenter__.return_value = usb
        before = UsbPort("/dev/cu.synthetic", (1,))
        current = UsbPort(before.path, (2,))
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", client_factory=lambda _: wifi)
            session.usb_port = before
            with patch.object(UsbPort, "_identity", return_value=current.node), \
                 patch("deskpet_platform.mcp_server.read_usb_identity") as read, \
                 patch.object(session, "usb_client", return_value=usb) as usb_client:
                read.return_value = UsbIdentity("02:00:00:00:00:02", uuid)
                with self.assertRaisesRegex(DeviceError, "device_identity_mismatch"):
                    await session.connect(config)
                usb_client.assert_not_called()
                self.assertIs(session.usb_port, before)
                read.return_value = UsbIdentity(DEVICE, uuid)
                self.assertIs(await session.connect(config), usb)
                usb_client.assert_called_once_with(config, current)
                self.assertEqual(session.usb_port, current)

    async def test_usb_fallback_waits_for_an_unplugged_port_and_checks_both_ids(self):
        uuid = "01020304-0506-4708-890a-0b0c0d0e0f10"
        config = DeviceConfig(DEVICE, KEY, device_id=uuid)
        path = "/dev/cu.synthetic-unplugged"
        wifi, usb = AsyncMock(), AsyncMock()
        wifi.__aenter__.side_effect = [wifi, OSError("wifi_absent"), OSError("wifi_absent"), OSError("wifi_absent")]
        usb.__aenter__.return_value = usb
        with tempfile.TemporaryDirectory() as directory, patch.object(
            UsbPort, "_identity", side_effect=FileNotFoundError,
        ) as node, patch("deskpet_platform.mcp_server.read_usb_identity") as read:
            session = BoardSession(Path(directory) / "device.json", client_factory=lambda _: wifi, usb_port=path)
            with patch.object(session, "usb_client", return_value=usb) as usb_client:
                self.assertIs(await session.connect(config), wifi)
                read.assert_not_called()
                node.side_effect = None
                node.return_value = (1,)
                registration = UsbPort(path, (1,))
                for other in (UsbIdentity(DEVICE, "01020304-0506-4708-890a-0b0c0d0e0f11"),
                              UsbIdentity("02:00:00:00:00:02", uuid)):
                    read.return_value = other
                    with self.assertRaisesRegex(DeviceError, "device_identity_mismatch"):
                        await session.connect(config)
                usb_client.assert_not_called()
                read.return_value = UsbIdentity(DEVICE, uuid)
                self.assertIs(await session.connect(config), usb)
                read.assert_called_with(registration)
                usb_client.assert_called_once_with(config, registration)
                self.assertEqual(session.usb_port, registration)

    async def test_usb_handoff_saves_the_verified_address_for_a_new_session(self):
        before = DeviceConfig(DEVICE, KEY, "192.168.1.41")
        after = DeviceConfig(DEVICE, KEY, "192.168.1.42")
        ready = {"wifi_ip": after.address, "wifi_handoff_ready": True}
        usb, wifi = AsyncMock(), AsyncMock()
        usb.config = before
        usb.channel.capabilities = {"wifi_probe": 1}
        usb.channel.dropped_events = 0
        usb.status.return_value = ready
        wifi.__aenter__.return_value = wifi
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.probe_board", new_callable=AsyncMock, return_value=True,
        ) as probe:
            path = Path(directory) / "device.json"
            before.save(path)
            session = BoardSession(path, client_factory=lambda config: wifi)
            session.usb_port = object()
            session.client = usb
            await session.recover_wifi(usb, ready)
            probe.assert_awaited_once_with(DEVICE, KEY, after.address)
            self.assertIs(session.client, wifi)
            self.assertEqual(DeviceConfig.load(path), after)

    async def test_token_status_stops_reporting_disconnected_and_stale_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", probe_collector=object())
            self.assertEqual(session.token_status(), {"status": "waiting"})
            session.token_snapshot = {"rate10": 600, "gear": 5}
            session.token_published_at = time.monotonic()
            self.assertEqual(session.token_status(), {"status": "ok", "rate10": 600, "gear": 5})
            session.token_published_at -= 15
            self.assertEqual(session.token_status(), {"status": "waiting"})
            session.token_published_at = time.monotonic()
            await session.drop()
            self.assertEqual(session.token_status(), {"status": "waiting"})

    async def test_reference_token_collector_fills_missing_quotas_from_local_codex(self):
        expires = int(time.time()) + 600
        local = [[75, 300, expires], [44, 10080, expires]]
        live = [[31, 300, expires]]
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.fetch_quota", new_callable=AsyncMock,
        ) as fetch:
            session = BoardSession(Path(directory) / "device.json")
            session.reference_collector = object()
            fetch.return_value = {"credits": "0", "quotas": [[12, 300, expires - 1000]]}
            with patch.object(session.token_metrics, "sample", return_value={"quotas": local}) as sample:
                await session.refresh_quota()
                self.assertEqual(session.codex_allowance, {"credits": "0", "quotas": local})
                fetch.return_value = {"quotas": live}
                await session.refresh_quota()
                self.assertEqual(session.codex_allowance, {"quotas": live})
                sample.assert_called_once_with()

    async def test_account_query_failure_keeps_last_result_and_retries_promptly(self):
        class Channel:
            capabilities = {"token_stats": 1}
        class Connected:
            channel = Channel()
        previous = {"credits": "12", "reset_cards": 2, "quotas": []}
        refreshed = {"credits": "11", "reset_cards": 1, "quotas": []}
        waits = []
        async def stop_after_retry(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                self.assertEqual(session.codex_allowance, previous)
            else:
                raise asyncio.CancelledError
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.fetch_quota", new_callable=AsyncMock, side_effect=[None, refreshed],
        ) as fetch, patch("deskpet_platform.mcp_server.asyncio.sleep", side_effect=stop_after_retry):
            session = BoardSession(Path(directory) / "device.json")
            session.client = Connected()
            session.codex_allowance = previous.copy()
            with self.assertRaises(asyncio.CancelledError):
                await session.poll_quota()
            self.assertEqual(fetch.await_count, 2)
            self.assertEqual(waits, [10, 300])
            self.assertEqual(session.codex_allowance, refreshed)

    async def test_device_status_reads_new_settings_and_uses_snapshot_during_card_work(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            client = AsyncMock()
            client.channel.capabilities = {"protocol": 1}
            client.link.wireless = False
            session.client = client
            session.snapshot = status_view({"screen_page": 0, "automatic_role": True},
                                           client.channel.capabilities)
            session.snapshot["notification_error"] = "fixture notification error"
            client.status.return_value = {"screen_page": 3, "automatic_role": False,
                                          "role_id": "new-role", "role_base_action": "dance_5"}

            async with session.storage_lock:
                self.assertEqual((await session.get_status())["screen_page"], "role")
                client.status.assert_not_awaited()

            status = await session.get_status()
            self.assertEqual(status["screen_page"], "settings")
            self.assertEqual(status["transport"], "usb")
            self.assertIs(status["automatic_role"], False)
            self.assertEqual(status["role"]["id"], "new-role")
            self.assertEqual(status["role"]["base_action"], "dance_5")
            self.assertEqual(status["notification_error"], "fixture notification error")

    async def test_device_status_preserves_rejected_link_and_drops_disconnected_link(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            client = AsyncMock()
            client.channel.dropped_events = 0
            client.status.side_effect = [CommandRejected("fixture_busy"), OSError("fixture_disconnected")]
            session.client = client
            session.snapshot = {"connection": "connected"}

            self.assertEqual((await session.get_status())["device_error"], "fixture_busy")
            client.close.assert_not_awaited()
            self.assertEqual((await session.get_status())["connection"], "offline")
            client.close.assert_awaited_once()
            self.assertIsNone(session.client)

    async def test_expired_control_session_is_dropped_and_poll_can_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "device.json"
            DeviceConfig(DEVICE, KEY, "192.168.1.42").save(config_path)
            session = BoardSession(config_path)
            expired, replacement = AsyncMock(), AsyncMock()
            for client in (expired, replacement):
                client.channel.capabilities = {}
                client.channel.dropped_events = 0
                client.link.wireless = True
            expired.status.side_effect = CommandRejected("handshake_required")
            replacement.status.return_value = {"screen_page": 0}
            session.client = expired
            session.snapshot = {"connection": "connected"}

            self.assertEqual((await session.get_status())["connection"], "offline")
            expired.close.assert_awaited_once()
            self.assertIsNone(session.client)

            # Polling must also recover when it sees the stale epoch first.
            session.client = expired
            expired.close.reset_mock()
            with patch.object(session, "connect", return_value=replacement) as connect, patch(
                "deskpet_platform.mcp_server.asyncio.sleep",
                side_effect=[None, asyncio.CancelledError],
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await session.poll()
            expired.close.assert_awaited_once()
            connect.assert_awaited_once()
            self.assertIs(session.client, replacement)
            self.assertEqual(session.snapshot["connection"], "connected")

    async def test_usb_pairing_verifies_over_usb_when_the_reported_wifi_address_is_unreachable(self):
        config = DeviceConfig(DEVICE, KEY, "192.168.1.42")
        wifi, usb = AsyncMock(), AsyncMock()
        wifi.__aenter__.side_effect = OSError("fixture_wifi_unreachable")
        usb.__aenter__.return_value = usb
        usb.device_id = None  # Legacy firmware does not report a UUID.
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.UsbPort.register",
        ) as register, patch("deskpet_platform.mcp_server.probe_usb", return_value=config):
            config_path = Path(directory) / "device.json"
            session = BoardSession(config_path, client_factory=lambda _: wifi)
            with patch.object(session, "usb_client", return_value=usb) as usb_client:
                result = await session.pair_usb("/dev/fixture", DEVICE, None)
            self.assertEqual(result, {"device": DEVICE})
            wifi.__aenter__.assert_awaited_once()
            usb_client.assert_called_once_with(config, register.return_value)
            usb.status.assert_awaited_once()
            self.assertEqual(DeviceConfig.load(config_path), config)
            self.assertIs(session.usb_port, register.return_value)

    async def test_usb_pairing_saves_the_authenticated_device_uuid(self):
        config = DeviceConfig(DEVICE, KEY, None)
        device_id = "01020304-0506-4708-890a-0b0c0d0e0f10"
        usb = AsyncMock()
        usb.__aenter__.return_value = usb
        usb.device_id = device_id
        connected = AsyncMock()
        connected.channel.dropped_events = 0

        def provision(*_):
            connected.close.assert_awaited_once()
            return config

        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.UsbPort.register",
        ) as register, patch("deskpet_platform.mcp_server.probe_usb", side_effect=provision):
            config_path = Path(directory) / "device.json"
            session = BoardSession(config_path)
            session.client = connected
            with patch.object(session, "usb_client", return_value=usb):
                result = await session.pair_usb("/dev/fixture", DEVICE, None)
            self.assertEqual(result, {"device": DEVICE, "device_id": device_id})
            self.assertEqual(DeviceConfig.load(config_path).device_id, device_id)
            self.assertIs(session.usb_port, register.return_value)

    async def test_saved_network_result_survives_reconnect_and_legacy_device(self):
        class NetworkChannel:
            capabilities = {"device_diagnostics": 1}
            selection = {"network_status": "switching", "boot": 11}
            card = {"card_ready": True, "spi_hz": 20_000_000}

            async def send(self, command, **fields):
                if command == "network_select":
                    assert fields == {"ssid": "Fixture two"}
                    return self.selection
                if command == "card_status":
                    return self.card
                assert command == "network_status"
                return {"wifi_connected": True, "ssid": "Fixture one",
                        "network_switch": {"operation": 7, "boot": 11, "status": "restored",
                                           "target": "Fixture two"}}

        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            client = Client(DeviceConfig(DEVICE, KEY))
            session.client = client
            client.channel = NetworkChannel()
            self.assertEqual(await session.select_network("Fixture two"), {"status": "switching"})
            client.channel.selection = {**client.channel.selection, "operation": 7}
            accepted = await session.select_network("Fixture two")
            self.assertEqual(accepted, {"status": "switching", "operation": 7, "boot": 11})
            client.channel = NetworkChannel()
            restored = await session.diagnostics(True)
            self.assertEqual(restored["ssid"], "Fixture one")
            self.assertEqual(restored["network_switch"], {"operation": 7, "boot": 11, "status": "restored",
                                                         "target": "Fixture two"})
            self.assertEqual(restored["card"], {"card_ready": True, "spi_hz": 20_000_000})
            client.channel.card = {**client.channel.card, "mount_count": 2}
            self.assertEqual((await session.diagnostics(True))["card"]["mount_count"], 2)

    async def test_display_diagnostics_reads_all_bounded_network_pages(self):
        names = ["网络一", 'Quote " and \\', "Control \u0001", *(f"Fixture {i}" for i in range(13))]
        encoded = [name.encode().hex() for name in names]

        class DisplayChannel:
            capabilities = {"device_diagnostics": 1, "network_profiles_hex": 1}

            def __init__(self):
                self.offsets = []
                self.duplicate = False

            async def send(self, command, **fields):
                if command == "network_status":
                    return {"wifi_connected": True, "ssid_hex": encoded[0], "saved_count": 16,
                            "network_switch": {"operation": 3, "target_hex": encoded[1]}}
                if command != "network_profiles":
                    raise AssertionError(command)
                offset = fields["offset"]
                self.offsets.append(offset)
                page = encoded[offset:offset + 8]
                if self.duplicate and offset:
                    page = [encoded[0], *page[1:]]
                return {"saved_networks_hex": page, "saved_count": 16, "next_offset": offset + len(page)}

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = DisplayChannel()
        result = await client.diagnostics(include_card=False)
        self.assertEqual(client.channel.offsets, [0, 8])
        self.assertEqual(result["saved_networks"], names)
        self.assertEqual(result["ssid"], names[0])
        self.assertEqual(result["network_switch"], {"operation": 3, "target": names[1]})
        client.channel.duplicate = True
        with self.assertRaisesRegex(DeviceError, "invalid_network_profiles_response"):
            await client.diagnostics(include_card=False)

    async def test_render_metrics_reports_written_frames_separately_from_processed_frames(self):
        class MetricsClient:
            class Channel:
                capabilities = {"render_metrics": 1}

            channel = Channel()
            reply = {"elapsed_ms": 1000, "frames": 12}

            async def render_metrics(self, *, reset):
                return self.reply

        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = MetricsClient()
            legacy = await session.render_metrics(False)
            self.assertEqual(legacy["fps"], 12)
            self.assertNotIn("displayed_fps", legacy)
            session.client.reply = {**session.client.reply, "displayed_frames": 10,
                                    "displayed_frame_gap_ms": [9, 900, 120], "spi_us": [10, 240000, 25000],
                                    "touch_dispatch_ms": [2, 95, 50], "touch_role_feedback_ms": [1, 100, 100],
                                    "loop_stack_min_bytes": 2048, "heap_free_bytes": 63000,
                                    "heap_largest_bytes": 33000}
            measured = await session.render_metrics(False)
            self.assertEqual(measured["displayed_fps"], 10)
            self.assertEqual(measured["displayed_frame_gap_ms"]["max"], 120)
            self.assertEqual(measured["spi_us"]["mean"], 24000)
            self.assertEqual(measured["touch_dispatch_ms"]["mean"], 47.5)
            self.assertEqual(measured["touch_role_feedback_ms"]["max"], 100)
            self.assertEqual(measured["loop_stack_min_bytes"], 2048)
            self.assertEqual(measured["heap_free_bytes"], 63000)
            self.assertEqual(measured["heap_largest_bytes"], 33000)

    async def test_background_weather_preserves_device_location_and_bootstraps_missing_location(self):
        location = {"latitude": 0.0, "longitude": 0.0, "label": "Fixture"}
        forecast = {"label": "Fixture", "updated": 1798731000}
        client = AsyncMock()
        client.channel.capabilities = {"weather": 1, "weather_device": 1}
        client.set_weather.return_value = {"updated": forecast["updated"]}
        with tempfile.TemporaryDirectory() as directory, patch(
            "deskpet_platform.mcp_server.fetch_weather", return_value=forecast,
        ) as fetch:
            session = BoardSession(Path(directory) / "device.json")
            session.client = client
            session.weather_path.write_text(json.dumps(location))
            session.record_status({"weather_device_refresh": True})
            await session.refresh_weather()
            fetch.assert_not_called()

            session.record_status({"weather_device_refresh": False})
            client.status.return_value = {"weather_device_refresh": True}
            await session.refresh_weather()
            client.set_weather.assert_not_awaited()

            session.weather_due = 0
            client.status.return_value = {"weather_device_refresh": False}
            await session.refresh_weather()
            client.set_weather.assert_awaited_once_with(forecast, location=location)

            session.weather_due = 0
            client.channel.capabilities = {"weather": 1}
            session.record_status({})
            await session.refresh_weather()
            self.assertEqual(client.set_weather.await_count, 2)
            self.assertEqual(client.status.await_count, 2)

    async def test_weather_location_is_sent_only_to_firmware_that_can_refresh(self):
        class WeatherChannel:
            def __init__(self):
                self.capabilities = {"weather": 1}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = WeatherChannel()
        snapshot = {"label": "Fixture", "updated": 1798731000}
        location = {"label": "Fixture", "latitude": 0.0, "longitude": 0.0}
        self.assertEqual(await client.set_weather(snapshot, location=location), ("weather_set", snapshot))
        self.assertNotIn("weather", status_view({}, client.channel.capabilities))
        client.channel.capabilities["weather_device"] = 1
        self.assertEqual(await client.set_weather(snapshot, location=location),
                         ("weather_set", {**snapshot, "location": {"latitude": 0.0, "longitude": 0.0}}))
        status = status_view({"weather_updated": 1798731000, "weather_device_refresh": True},
                             client.channel.capabilities)
        self.assertEqual(status["weather"], {"updated": 1798731000, "device_refresh": True})
        offline = status_view({"weather_updated": 1798731000, "weather_device_refresh": True,
                               "weather_cached": True, "weather_cache_indicator": True}, client.channel.capabilities)
        self.assertEqual(offline["weather"], {"updated": 1798731000, "device_refresh": True,
                                              "cached": True, "cached_on_screen": True})

    async def test_role_action_discovery_can_page_over_thirty_names(self):
        names = [f"action_{index}" for index in range(48)]
        class Channel:
            capabilities = {"role_actions": 1}
            async def send(self, command, **values):
                self_command = command
                assert self_command == "role_action_list"
                offset = values["offset"]
                chunk = names[offset:offset + 6]
                next_offset = offset + len(chunk)
                return {"actions": chunk, "next_offset": next_offset if next_offset < len(names) else -1}
        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        self.assertEqual(await client.list_actions(), names)

    async def test_role_preview_sdk_uses_capability_and_validates_the_target(self):
        class Channel:
            capabilities = {}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        with self.assertRaisesRegex(DeviceError, "role_preview_not_supported"):
            await client.role_preview("status")
        client.channel.capabilities["role_preview"] = 1
        self.assertEqual(await client.role_preview("start", "sample-role"),
                         ("role_preview", {"mode": "start", "id": "sample-role"}))
        self.assertEqual(await client.role_preview("use"), ("role_preview", {"mode": "use"}))
        for mode, identity in (("start", None), ("start", "../sample"), ("cancel", "sample-role")):
            with self.assertRaises(ValueError):
                await client.role_preview(mode, identity)

    async def test_preview_use_reports_success_only_after_async_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            client = AsyncMock()
            client.channel.capabilities = {"role_preview": 1}
            client.role_preview.return_value = {
                "preview_status": "selected", "role_state": "loading", "id": "sample-role",
            }
            session.client = client
            failed = {"status": "activation_failed", "role": "sample-role"}
            with patch.object(session, "wait_role", side_effect=[failed, {"status": "ready"}]) as wait:
                self.assertEqual(await session.role_preview("use", None), failed)
                self.assertEqual(await session.role_preview("use", None),
                                 {"status": "selected", "role": "sample-role"})
                self.assertEqual([call.args for call in wait.await_args_list],
                                 [("sample-role",), ("sample-role",)])

    async def test_photo_album_sdk_checks_mode_arguments_and_device_support(self):
        class Channel:
            capabilities = {}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        with self.assertRaisesRegex(DeviceError, "photo_album_not_supported"):
            await client.photo_album()
        client.channel.capabilities["photo_album"] = 1
        identity = "p-0123456789abcdef"
        self.assertEqual(await client.photo_album("open", photo=identity),
                         ("photo_album", {"mode": "open", "id": identity}))
        self.assertEqual(await client.photo_album("interval", interval_seconds=5),
                         ("photo_album", {"mode": "interval", "seconds": 5}))
        for mode, fields in (("open", {}), ("open", {"photo": "../x"}),
                             ("interval", {"interval_seconds": 8}), ("pause", {"photo": identity})):
            with self.assertRaises(ValueError):
                await client.photo_album(mode, **fields)

    async def test_message_view_sdk_requires_an_explicit_id_for_marking_seen(self):
        class Channel:
            capabilities = {}

            async def send(self, command, **fields):
                return command, fields

        client = Client(DeviceConfig(DEVICE, KEY))
        client.channel = Channel()
        with self.assertRaisesRegex(DeviceError, "message_view_not_supported"):
            await client.message_view()
        client.channel.capabilities["message_view"] = 1
        self.assertEqual(await client.message_view("open", 27), ("message_view", {"mode": "open", "id": 27}))
        self.assertEqual(await client.message_view("mark_seen", 27),
                         ("message_view", {"mode": "mark_seen", "id": 27}))
        for mode, identity in (("mark_seen", None), ("mark_seen", True), ("open", 0), ("list", 27)):
            with self.assertRaises(ValueError):
                await client.message_view(mode, identity)

    async def test_handshake_display_and_disconnect_on_local_wire(self):
        observed = asyncio.get_running_loop().create_future()
        tasks = set()

        async def board(packet):
            try:
                marker, identity, port = packet.decode().split(",")
                self.assertEqual((marker, identity), ("DESK-LINK-1", DEVICE))
                reader, writer = await asyncio.open_connection("127.0.0.1", int(port))
                try:
                    _, _, host_nonce = (await reader.readline()).decode().strip().split(",")
                    nonce = "0123456789abcdef" * 2
                    signature = proof(KEY, "device", DEVICE, host_nonce, nonce)
                    writer.write(f"D1,{nonce},{signature}\n".encode())
                    await writer.drain()
                    expected = proof(KEY, "host", DEVICE, host_nonce, nonce)
                    self.assertEqual(await reader.readline(), f"H2,{expected}\n".encode())
                    codec = RecordCodec(session_key(KEY, DEVICE, host_nonce, nonce), b"DEVC", b"HOST")
                    parts = []
                    generation = 0
                    text_status = "none"
                    checksum = 0
                    tap_sent = False
                    tap_acked = False
                    uploaded = {}
                    role_id = None
                    actions = ["idle", "wave"]
                    while line := await reader.readline():
                        request = json.loads(codec.decode(line)[3:])
                        reply = dict(epoch=request["epoch"], seq=request["seq"], boot=1, ok=True)
                        match request["type"]:
                            case "hello":
                                reply.update(mac=DEVICE, protocol=1, rx_bytes=2048, text_present=1, touch_demo=1,
                                             card_transfer=1, role_actions=1, role_base=1, image_display=1, brightness_control=1)
                            case "heartbeat":
                                reply.update(pet_page=0, view_token=7, text_generation=generation, text_status=text_status)
                                if role_id:
                                    reply.update(role_state="ready", role_id=role_id, role_action_count=len(actions))
                                if text_status == "shown" and not tap_sent:
                                    event = dict(type="touch", epoch=request["epoch"], boot=1, operation=7,
                                                 version=0, id="screen", action="tap", x=125, y=64)
                                    writer.write(codec.encode(b"P1," + json.dumps(event).encode() + b"\n"))
                                    tap_sent = True
                            case "text_begin":
                                generation = request["generation"]
                                checksum = request["crc32"]
                                self.assertEqual(request["bytes"], 1728)
                                self.assertEqual(request["view_token"], 7)
                                reply["text_status"] = "receiving"
                            case "text_chunk":
                                self.assertEqual(request["index"], len(parts))
                                parts.append(bytes.fromhex(request["data"]))
                                reply["text_status"] = "receiving"
                            case "text_commit":
                                self.assertEqual(len(parts), 9)
                                self.assertEqual(zlib.crc32(b"".join(parts)), checksum)
                                reply["text_status"] = "ready"
                                text_status = "shown"
                            case "touch_ack":
                                self.assertEqual((request["operation"], request["status"]), (7, "accepted"))
                                tap_acked = True
                            case "card_fetch":
                                def fetch():
                                    with urllib.request.urlopen(request["url"]) as response:
                                        return response.read()
                                ciphertext = await asyncio.to_thread(fetch)
                                key = hmac.new(KEY, b"desk-card-v1/key", hashlib.sha256).digest()
                                aad = f'desk-card-v1\n{request["path"]}\n{request["bytes"]}\n{request["crc32"]:08x}'.encode()
                                data = AESGCM(key).decrypt(bytes.fromhex(request["iv"]), ciphertext, aad)
                                if request["path"].startswith("desk/images/"):
                                    self.assertEqual(len(data), 320 * 240 * 2)
                                elif request["path"] == "roles/test/v1/frames.bin":
                                    self.assertEqual(data, b"synthetic role data")
                                uploaded[request["path"]] = data
                                reply["stored"] = True
                            case "role_list":
                                reply.update(roles=["synthetic-role"], next_offset=-1)
                            case "role_action_list":
                                offset = request["offset"]
                                part = actions[offset:offset + 6]
                                reply.update(actions=part, next_offset=offset + len(part) if offset + len(part) < len(actions) else -1)
                            case "role_prepare":
                                role_id, actions = request["id"], request["actions"]
                                prefix = f'roles/{role_id}/{request["version"]}/pet-v2/'
                                self.assertIn(prefix + "frames.bin", uploaded)
                                self.assertIn(prefix + "timeline-v2.bin", uploaded)
                                reply["role_state"] = "loading"
                            case "role_play":
                                self.assertLess(request["action"], len(actions))
                                reply["action_status"] = "started"
                            case "role_base":
                                self.assertLess(request["action"], len(actions))
                                reply["base_status"] = "set"
                            case "image_show":
                                reply["image_status"] = "shown"
                            case "image_clear":
                                reply["image_status"] = "cleared"
                            case "brightness_set":
                                reply["brightness"] = request["level"]
                        writer.write(codec.encode(b"P1," + json.dumps(reply).encode() + b"\n"))
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                observed.set_result(tap_acked)
            except Exception as error:
                observed.set_exception(error)

        class Discovery(asyncio.DatagramProtocol):
            def datagram_received(self, packet, address):
                if not tasks:
                    tasks.add(asyncio.create_task(board(packet)))

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Discovery, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                def link(identity, key, peer_hint):
                    return WifiLink(identity, key, lock_root=directory, discovery_host="127.0.0.1", discovery_port=port)

                async with Client(DeviceConfig(DEVICE, KEY), link_factory=link) as client:
                    self.assertEqual((await client.status())["pet_page"], 0)
                    self.assertEqual(await client.show_text("Hello"), {"generation": 1, "status": "shown"})
                    touch = await client.next_touch(timeout=2)
                    self.assertEqual((touch["action"], touch["x"], touch["y"]), ("tap", 125, 64))
                    source = Path(directory) / "synthetic.bin"
                    source.write_bytes(b"synthetic role data")
                    result = await client.upload_file("roles/test/v1/frames.bin", source)
                    self.assertEqual(result["bytes"], len(b"synthetic role data"))
                    self.assertEqual(await client.list_roles(), ["synthetic-role"])
                    self.assertEqual(await client.list_actions(), ["idle", "wave"])
                    role = Path(directory) / "sample-role"
                    role_pack = role / "pet-v2"
                    role_pack.mkdir(parents=True)
                    make_pack(role_pack)
                    write_role_actions(role)
                    session = BoardSession(Path(directory) / "config.json")
                    session.client = client
                    installed = await session.install_role(str(role))
                    self.assertEqual(installed["status"], "ready")
                    self.assertEqual(len(await client.list_actions()), 12)
                    self.assertEqual((await session.play_role("wake", 2000))["status"], "started")
                    self.assertEqual((await session.set_role_base("wake"))["status"], "set")
                    self.assertEqual((await session.set_role_base("missing"))["status"], "unsupported")
                    extended = Path(directory) / "extended-role"
                    extended_pack = extended / "pet-v2"
                    extended_pack.mkdir(parents=True)
                    make_pack(extended_pack, width=4, height=3, actions=48)
                    names = [f"action_{index}" for index in range(48)]
                    (extended / "actions.txt").write_text("\n".join(names) + "\n")
                    self.assertEqual((await session.install_role(str(extended)))["status"], "ready")
                    self.assertEqual(await client.list_actions(), names)
                    self.assertEqual((await session.set_role_base("action_47"))["status"], "set")
                    picture = Path(directory) / "picture.png"
                    Image.new("RGB", (12, 12), "red").save(picture)
                    self.assertEqual((await client.show_image(picture))["status"], "shown")
                    self.assertEqual((await client.clear_image())["status"], "cleared")
                    self.assertEqual(await client.set_brightness(60), 60)
                self.assertTrue(await asyncio.wait_for(observed, 2))
        finally:
            transport.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_real_mcp_starts_with_unplugged_usb_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port = Path("/dev") / ("cu.deskpet-unplugged-" + root.name)
            self.assertFalse(port.exists())
            command = StdioServerParameters(command=sys.executable, args=[
                "-m", "deskpet_platform", "--config", str(root / "device.json"),
                "mcp", "--usb-port", str(port), "--no-token-probe",
            ], env={"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex")})
            async with asyncio.timeout(10):
                async with stdio_client(command) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        listed = await session.call_tool("desk_list_devices")
                        self.assertEqual(listed.structuredContent, {"devices": []})
                        result = await session.call_tool("desk_find_usb_device", {"ports": [str(port)]})
                        self.assertFalse(result.isError)
                        self.assertEqual(result.structuredContent["devices"], [])
                        self.assertEqual([item["port"] for item in result.structuredContent["unavailable"]],
                                         [str(port)])

    async def test_real_mcp_lists_and_reads_manuals_without_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            role_dir = Path(directory) / "roles"
            role_pack = role_dir / "sample-role" / "pet-v2"
            role_pack.mkdir(parents=True)
            make_pack(role_pack)
            command = StdioServerParameters(command=sys.executable, args=[
                "-m", "deskpet_platform", "--config", str(Path(directory) / "device.json"),
                "mcp", "--role-dir", str(role_dir),
            ], env={"XDG_CONFIG_HOME": str(Path(directory) / "config"), "HOME": str(Path(directory) / "home")})
            async with asyncio.timeout(10):
                async with stdio_client(command) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertIn("topic=roles", initialized.instructions)
                        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                        self.assertEqual(set(tools), {"desk_read_manual", "desk_list_devices", "desk_find_usb_device", "desk_set_usb_fallback", "desk_show_text", "desk_status", "desk_render_metrics", "desk_computer_metrics",
                                                 "desk_sync_clock", "desk_clock_edit", "desk_set_view", "desk_add_photo", "desk_photo_album", "desk_set_weather_location",
                                                 "desk_token_status", "desk_speed_windows", "desk_set_role_auto",
                                                 "desk_diagnostics", "desk_select_network",
                                                 "desk_pair_begin", "desk_pair_usb", "desk_pair_status", "desk_upload_files", "desk_delete_resource",
                                                 "desk_create_role_example", "desk_check_role", "desk_install_role", "desk_select_role", "desk_role_preview", "desk_list_actions", "desk_play_action", "desk_action_status", "desk_set_role_base",
                                                 "desk_list_roles", "desk_list_role_sources", "desk_wait_touch", "desk_show_image", "desk_clear_image",
                                                 "desk_set_brightness", "desk_calibrate_touch", "desk_cancel_touch_calibration",
                                                 "desk_enable_codex_notifications", "desk_disable_codex_notifications",
                                                 "desk_set_claude_notifications", "desk_set_antigravity_notifications",
                                                 "desk_set_opencode_notifications", "desk_set_workbuddy_notifications",
                                                 "desk_list_notifications", "desk_mark_notification_seen", "desk_message_view"})
                        for name in ("desk_list_devices", "desk_read_manual"):
                            self.assertIs(tools[name].annotations.readOnlyHint, True)
                            self.assertIs(tools[name].annotations.openWorldHint, False)
                        for name in ("desk_status", "desk_show_text", "desk_pair_usb"):
                            annotations = tools[name].annotations
                            self.assertFalse(annotations and annotations.readOnlyHint)
                        resources = {str(resource.uri) for resource in (await session.list_resources()).resources}
                        for topic, phrase in (("operations", "操作手册"), ("development", "二次开发手册"),
                                              ("roles", "添加自定义角色")):
                            result = await session.call_tool("desk_read_manual", {"topic": topic})
                            self.assertFalse(result.isError)
                            self.assertIn(phrase, result.content[0].text)
                            uri = "deskpet://manual/" + topic
                            self.assertIn(uri, resources)
                            resource = await session.read_resource(uri)
                            self.assertEqual(resource.contents[0].text, result.content[0].text)
                        status = await session.call_tool("desk_status")
                        self.assertEqual(status.structuredContent["connection"], "not_paired")
                        configured = await session.call_tool("desk_set_opencode_notifications", {"enabled": True})
                        self.assertFalse(configured.isError)
                        plugin = Path(configured.structuredContent["config"])
                        self.assertEqual(plugin.parent, Path(directory) / "config" / "opencode" / "plugins")
                        self.assertTrue(plugin.is_file())
                        disabled = await session.call_tool("desk_set_opencode_notifications", {"enabled": False})
                        self.assertFalse(disabled.isError)
                        self.assertFalse(plugin.exists())
                        settings = Path(directory) / "home/.workbuddy-ai/settings.json"
                        settings.parent.mkdir(parents=True)
                        existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]}}
                        settings.write_text(json.dumps(existing))
                        workbuddy = {"edition": "workbuddy_ai", "enabled": True}
                        configured = await session.call_tool("desk_set_workbuddy_notifications", workbuddy)
                        self.assertFalse(configured.isError)
                        self.assertEqual(Path(configured.structuredContent["config"]), settings)
                        hooks = json.loads(settings.read_text())["hooks"]
                        self.assertEqual(hooks["Stop"][0], existing["hooks"]["Stop"][0])
                        self.assertTrue(hooks["Stop"][1]["hooks"][0]["command"].endswith("workbuddy-hook --edition workbuddy_ai"))
                        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "AskUserQuestion")
                        self.assertEqual(hooks["Notification"][0]["matcher"], "^permission_prompt$")
                        disabled = await session.call_tool("desk_set_workbuddy_notifications", {**workbuddy, "enabled": False})
                        self.assertFalse(disabled.isError)
                        self.assertEqual(json.loads(settings.read_text()), existing)
                        preview = await session.call_tool("desk_role_preview")
                        self.assertEqual(preview.structuredContent["status"], "not_connected")
                        album = await session.call_tool("desk_photo_album")
                        self.assertEqual(album.structuredContent["status"], "not_connected")
                        sources = await session.call_tool("desk_list_role_sources")
                        self.assertEqual(sources.structuredContent["roles"], [{
                            "id": "sample-role", "path": str(role_pack.parent), "format": "PET1",
                        }])
                        path = role_dir / "Pocket Bot.zip"
                        example = await session.call_tool("desk_create_role_example", {"destination": str(path)})
                        self.assertFalse(example.isError, example.content)
                        self.assertEqual(example.structuredContent["path"], str(path))
                        self.assertEqual(example.structuredContent["id"], "user-pocket-bot")
                        original = path.read_bytes()
                        repeated = await session.call_tool("desk_create_role_example", {"destination": str(path)})
                        self.assertTrue(repeated.isError)
                        self.assertEqual(path.read_bytes(), original)
                        sources = await session.call_tool("desk_list_role_sources")
                        self.assertIn({"id": "user-pocket-bot", "name": "Pocket Bot", "path": str(path),
                                       "format": "role1"}, sources.structuredContent["roles"])
                        checked = await session.call_tool("desk_check_role", {"source": str(path)})
                        self.assertFalse(checked.isError, checked.content)
                        self.assertEqual(checked.structuredContent["status"], "valid")
                        self.assertEqual(checked.structuredContent["role"], "user-pocket-bot")
                        self.assertIn("touched", checked.structuredContent["actions"])
                        malformed = role_dir / "renamed.zip"
                        with ZipFile(path) as original_zip, ZipFile(malformed, "w") as renamed_zip:
                            for name in original_zip.namelist():
                                renamed = "Row.565" if name == "row.565" else name
                                renamed_zip.writestr(renamed, original_zip.read(name))
                        result = await session.call_tool("desk_check_role", {"source": str(malformed)})
                        self.assertEqual(result.structuredContent["status"], "invalid")
                        self.assertIn("缺少", result.structuredContent["reason"])
                        self.assertIn("row.565", result.structuredContent["reason"])
                        self.assertIn("Row.565", result.structuredContent["reason"])
                        install = await session.call_tool("desk_install_role", {"source": str(malformed)})
                        self.assertTrue(install.isError)
                        self.assertIn(result.structuredContent["reason"], install.content[0].text)
                        result = await session.call_tool("desk_install_role", {"source": str(path)})
                        self.assertEqual(result.structuredContent["status"], "not_connected")
                        result = await session.call_tool("desk_pair_begin", {"open_browser": False})
                        self.assertTrue(result.structuredContent["url"].startswith("http://127.0.0.1:"))
                        self.assertFalse(result.structuredContent["browser_opened"])
                        result = await session.call_tool("desk_pair_status")
                        self.assertEqual(result.structuredContent["state"], "waiting")
                        result = await session.call_tool("desk_upload_files", {"files": []})
                        self.assertTrue(result.isError)
                        result = await session.call_tool("desk_upload_files", {"files": [
                            {"source": str(path), "path": "roles/user-pocket-bot/active"},
                        ]})
                        self.assertTrue(result.isError)
                        self.assertIn("desk_install_role", result.content[0].text)

    def test_pairing_credentials_are_local_and_private(self):
        code = "D1-" + DEVICE.replace(":", "") + "-" + base64.urlsafe_b64encode(KEY).decode().rstrip("=")
        config = DeviceConfig.from_code(code, "192.168.1.100")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.json"
            config.save(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(DeviceConfig.load(path), config)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                DeviceConfig.load(path)
        self.assertNotIn(repr(KEY), repr(config))
        with self.assertRaises(ValueError):
            DeviceConfig.from_code(code, "8.8.8.8")
