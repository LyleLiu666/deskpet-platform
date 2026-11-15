"""Rehearse the local pairing page against a synthetic authenticated board."""
import asyncio
import base64
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import unittest

from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.pair_portal import PairPortal
from test_multi_device_flow import FIRST, SyntheticBoard


class PairingRehearsalTests(unittest.IsolatedAsyncioTestCase):
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
