from concurrent.futures import ThreadPoolExecutor
from threading import Event
import urllib.error
import urllib.parse
import urllib.request
import unittest
from unittest.mock import patch

from deskpet_platform.pair_portal import PairPortal


class PairPortalTests(unittest.TestCase):
    def test_opening_a_loopback_portal_does_not_resolve_a_hostname(self):
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected hostname lookup")):
            portal = PairPortal(None).start()
        self.addCleanup(portal.close)
        self.assertEqual(urllib.parse.urlsplit(portal.url).hostname, "127.0.0.1")
        with urllib.request.urlopen(portal.url, timeout=5) as response:
            self.assertEqual(response.status, 200)

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
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read().decode()

        self.assertEqual(submit("valid-code", origin_header="http://evil.example")[0], 403)
        self.assertEqual(submit("valid-code", origin_header="null")[0], 403)
        self.assertEqual(calls, [])
        status, retry = submit("invalid-credential")
        self.assertEqual(status, 400)
        self.assertIn('<form method="post">', retry)
        self.assertIn('role="alert"', retry)
        self.assertIn('name="address"', retry)
        self.assertNotIn("invalid-credential", retry)
        self.assertEqual(portal.status()["state"], "waiting")
        self.assertEqual(submit("valid-code")[0], 200)
        self.assertEqual(calls, [("invalid-credential", None), ("valid-code", None)])
        self.assertEqual(portal.status(), {"state": "paired", "device": "synthetic"})
        self.assertEqual(submit("valid-code")[0], 410)

    def test_discovery_failure_keeps_ip_field_and_allows_retry_at_the_same_address(self):
        seen = []

        def pair(code, address):
            seen.append(address)
            if address is None:
                raise OSError("wifi_device_absent")
            return {"device": "synthetic"}

        portal = PairPortal(pair).start()
        self.addCleanup(portal.close)
        origin = urllib.parse.urljoin(portal.url, "/").rstrip("/")

        def request(address):
            return urllib.request.Request(portal.url,
                data=urllib.parse.urlencode({"code": "test-code", "address": address}).encode(),
                headers={"Origin": origin})

        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request(""))
        with error.exception as failure:
            self.assertEqual(failure.code, 400)
            self.assertEqual(failure.headers["Cache-Control"], "no-store")
            retry = failure.read().decode()
        self.assertIn("没有收到搭搭", retry)
        self.assertIn('<form method="post">', retry)
        with urllib.request.urlopen(request("192.168.1.2")) as response:
            self.assertEqual(response.status, 200)
        with urllib.request.urlopen(portal.url) as response:
            body = response.read().decode()
            self.assertIn("已验证并保存", body)
            self.assertNotIn('name="code"', body)
        self.assertEqual(seen, [None, "192.168.1.2"])

    def test_expired_page_does_not_accept_credentials(self):
        calls = []
        portal = PairPortal(lambda code, address: calls.append(code), lifetime=60).start()
        self.addCleanup(portal.close)
        portal.created -= 61
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(portal.url)
        self.assertEqual(error.exception.code, 410)
        self.assertEqual(calls, [])

    def test_parallel_submission_cannot_replace_an_inflight_pairing(self):
        entered = Event()
        release = Event()
        calls = []

        def pair(code, address):
            calls.append(code)
            entered.set()
            self.assertTrue(release.wait(5))
            return {"device": "synthetic"}

        portal = PairPortal(pair).start()
        self.addCleanup(portal.close)
        origin = urllib.parse.urljoin(portal.url, "/").rstrip("/")

        def submit(code):
            request = urllib.request.Request(portal.url, data=urllib.parse.urlencode({"code": code}).encode(),
                                             headers={"Origin": origin})
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(submit, "first-test-code")
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    submit("second-test-code")
                with error.exception as busy:
                    self.assertEqual(busy.code, 409)
                    self.assertIn("正在验证搭搭", busy.read().decode())
                self.assertEqual(portal.status()["state"], "checking")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5), 200)
        self.assertEqual(calls, ["first-test-code"])
