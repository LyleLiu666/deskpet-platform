import json
import os
from pathlib import Path
import tempfile
import unittest

from deskpet_platform.pairing import DeviceConfig


@unittest.skipUnless(os.name == "nt", "Windows credential protection")
class WindowsPairingTests(unittest.TestCase):
    def test_pairing_is_encrypted_for_the_current_windows_user_and_round_trips(self):
        config = DeviceConfig("02:00:00:00:00:01", bytes(range(32)), "192.168.1.2")
        with tempfile.TemporaryDirectory(prefix="deskpet-pairing-") as directory:
            path = Path(directory) / "device.json"
            config.save(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(value), {"protected"})
            self.assertNotIn(config.key.hex(), path.read_text(encoding="utf-8"))
            self.assertNotIn(config.identity, path.read_text(encoding="utf-8"))
            self.assertEqual(DeviceConfig.load(path), config)
            value["protected"] = value["protected"][:-4] + "AAAA"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                DeviceConfig.load(path)

    def test_unprotected_plaintext_pairing_is_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="deskpet-pairing-") as directory:
            path = Path(directory) / "device.json"
            path.write_text(json.dumps({"identity":"02:00:00:00:00:01", "key":bytes(32).hex()}), encoding="utf-8")
            with self.assertRaises(ValueError):
                DeviceConfig.load(path)
