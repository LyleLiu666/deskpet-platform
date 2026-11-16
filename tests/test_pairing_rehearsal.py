"""Rehearse the local pairing page against a synthetic authenticated board."""
import asyncio
import base64
from pathlib import Path
import tempfile
from threading import Event
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession, create_server
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.pair_portal import PairPortal
from test_multi_device_flow import FIRST, SyntheticBoard


class PairingRehearsalTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairing_modes_cannot_replace_an_inflight_page(self):
        entered = Event()
        release = Event()
        opened = []

        class TrackedPortal(PairPortal):
            def start(self):
                opened.append(self)
                return super().start()

        def verify(code, address):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"device": "synthetic"}

        def submit(url):
            origin = urllib.parse.urljoin(url, "/").rstrip("/")
            request = urllib.request.Request(url, data=b"code=synthetic", headers={"Origin": origin})
            with urllib.request.urlopen(request, timeout=6) as response:
                return response.status

        with tempfile.TemporaryDirectory() as directory, \
             patch("deskpet_platform.mcp_server.PairPortal", TrackedPortal), \
             patch.object(BoardSession, "pair_from_portal", side_effect=verify):
            server = create_server(Path(directory) / "device.json", no_token_probe=True)
            options = {"open_browser": False}
            _, initial = await server.call_tool("desk_pair_begin", options)
            inflight = asyncio.create_task(asyncio.to_thread(submit, initial["url"]))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                _, same = await server.call_tool("desk_pair_begin", options)
                self.assertEqual(same["url"], initial["url"])
                with self.assertRaisesRegex(ToolError, "正在验证"):
                    await server.call_tool("desk_pair_begin", {**options, "add_device": True})
                self.assertEqual(len(opened), 1)
                _, status = await server.call_tool("desk_pair_status", {})
                self.assertEqual(status["state"], "checking")
                release.set()
                self.assertEqual(await inflight, 200)
                results = await asyncio.gather(*(
                    server.call_tool("desk_pair_begin", {**options, "add_device": True}) for _ in range(2)))
                self.assertEqual(len(opened), 2)
                self.assertEqual(results[0][1]["url"], results[1][1]["url"])
                opened[-1].created -= 120
                _, reused = await server.call_tool("desk_pair_begin", {**options, "add_device": True})
                self.assertEqual(reused["url"], results[0][1]["url"])
                self.assertLessEqual(reused["expires_in_seconds"], 180)
            finally:
                release.set()
                await asyncio.gather(inflight, return_exceptions=True)
                for portal in opened:
                    await asyncio.to_thread(portal.close)

    async def test_local_page_authenticates_then_saves_and_retries(self):
        with tempfile.TemporaryDirectory(prefix="deskpet pair rehearsal ") as root:
            path = Path(root) / "device.json"
            board = SyntheticBoard(FIRST)

            def link(identity, key, peer_hint=None):
                if identity != FIRST.identity or key != FIRST.key:
                    raise OSError("synthetic authentication failed")
                return board

            session = BoardSession(path, client_factory=lambda config: Client(config, link_factory=link),
                                   no_token_probe=True)
            session.loop = asyncio.get_running_loop()
            portal = PairPortal(session.pair_from_portal).start()
            self.addCleanup(portal.close)
            parsed = urllib.parse.urlsplit(portal.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"

            def submit(key):
                encoded = base64.urlsafe_b64encode(key).decode().rstrip("=")
                code = f"D1-{FIRST.identity.replace(':', '')}-{encoded}"
                body = urllib.parse.urlencode({"code": code, "address": ""}).encode()
                request = urllib.request.Request(portal.url, data=body, headers={"Origin": origin})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code

            self.assertEqual(await asyncio.to_thread(submit, bytes(32)), 400)
            self.assertFalse(path.exists())
            self.assertEqual(portal.status()["state"], "waiting")
            self.assertEqual(await asyncio.to_thread(submit, FIRST.key), 200)
            saved = DeviceConfig.load(path)
            self.assertEqual((saved.identity, saved.key, saved.device_id),
                             (FIRST.identity, FIRST.key, FIRST.device_id))
            self.assertEqual(portal.status()["state"], "paired")
            self.assertEqual(await asyncio.to_thread(submit, FIRST.key), 410)
            await session.drop()
