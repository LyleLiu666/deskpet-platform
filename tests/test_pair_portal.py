import urllib.error
import urllib.parse
import urllib.request
import unittest

from deskpet_platform.pair_portal import PairPortal


class PairPortalTests(unittest.TestCase):
    def test_pairing_is_local_protected_and_only_finishes_after_verification(self):
        calls = []

        def pair(code, address):
            calls.append((code, address))
            if code != "valid-code":
                raise ValueError("invalid")
            return {"device": "synthetic"}

        portal = PairPortal(pair).start()
        self.addCleanup(portal.close)
        parsed = urllib.parse.urlsplit(portal.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with urllib.request.urlopen(portal.url) as response:
            self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
            self.assertIn("配对码", response.read().decode())

        def submit(code, *, origin_header=origin):
            body = urllib.parse.urlencode({"code": code, "address": ""}).encode()
            request = urllib.request.Request(portal.url, data=body, headers={"Origin": origin_header})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        self.assertEqual(submit("valid-code", origin_header="http://evil.example"), 403)
        self.assertEqual(submit("valid-code", origin_header="null"), 403)
        self.assertEqual(calls, [])
        self.assertEqual(submit("wrong"), 400)
        self.assertEqual(portal.status()["state"], "waiting")
        self.assertEqual(submit("valid-code"), 200)
        self.assertEqual(calls, [("wrong", None), ("valid-code", None)])
        self.assertEqual(portal.status(), {"state": "paired", "device": "synthetic"})
        self.assertEqual(submit("valid-code"), 410)

    def test_expired_page_does_not_accept_credentials(self):
        calls = []
        portal = PairPortal(lambda code, address: calls.append(code), lifetime=60).start()
        self.addCleanup(portal.close)
        portal.created -= 61
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(portal.url)
        self.assertEqual(error.exception.code, 410)
        self.assertEqual(calls, [])
