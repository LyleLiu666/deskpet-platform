import tempfile
import unittest
from pathlib import Path

from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.pairing import DeviceConfig

KEY = bytes(range(32))
A = DeviceConfig('02:00:00:00:00:01', KEY)
B = DeviceConfig('02:00:00:00:00:02', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f10')
C = DeviceConfig('02:00:00:00:00:03', KEY, device_id='01020304-0506-4708-890a-0b0c0d0e0f11')


class DeviceRegistryTests(unittest.TestCase):
    def test_legacy_pairing_is_preserved_and_devices_are_addressed_individually(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            original = path.read_bytes()
            registry = DeviceRegistry(path)
            registry.add(B)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(registry.get('mac:' + A.identity), A)
            self.assertEqual(registry.get(B.device_id), B)
            self.assertEqual({item['id'] for item in registry.list()}, {'mac:' + A.identity, B.device_id})
            self.assertEqual((path.parent / 'devices' / B.device_id / 'device.json').stat().st_mode & 0o777, 0o600)

    def test_reject_identity_collisions_and_unknown_or_invalid_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            registry = DeviceRegistry(path)
            registry.add(B)
            for duplicate in (DeviceConfig(A.identity, KEY, device_id=C.device_id),
                              DeviceConfig(C.identity, KEY, device_id=B.device_id), B):
                with self.assertRaises(ValueError):
                    registry.add(duplicate)
            for unknown in (C.device_id, '../device', None, 'mac:invalid'):
                with self.assertRaises((ValueError, KeyError)):
                    registry.get(unknown)
            self.assertEqual(registry.get(B.device_id), B)

    def test_two_legacy_devices_use_mac_only_until_firmware_provides_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            legacy = DeviceConfig(C.identity, KEY)
            registry = DeviceRegistry(path)
            registry.add(legacy)
            self.assertEqual(registry.get('mac:' + C.identity), legacy)
            self.assertIsNone(registry.get('mac:' + C.identity).device_id)

    def test_existing_conflicting_entries_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            registry = DeviceRegistry(path)
            registry.add(B)
            # Simulate a copied or edited credential file rather than using add().
            conflicting = DeviceConfig(A.identity, KEY, device_id=C.device_id)
            conflicting.save(path.parent / 'devices' / C.device_id / 'device.json')
            with self.assertRaises(ValueError):
                registry.get(B.device_id)
            with self.assertRaises(ValueError):
                registry.list()

    def test_upgrade_legacy_identity_only_after_observation_without_moving_its_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'device.json'
            A.save(path)
            registry = DeviceRegistry(path)
            legacy = DeviceConfig(C.identity, KEY)
            registry.add(legacy)
            old_id = 'mac:' + C.identity
            old_path = registry.path_for(old_id)
            upgraded = registry.upgrade(old_path, legacy, C.device_id)
            self.assertEqual(upgraded.device_id, C.device_id)
            self.assertEqual(registry.path_for(C.device_id), old_path)
            with self.assertRaises(KeyError):
                registry.get(old_id)
            with self.assertRaises(ValueError):
                registry.upgrade(path, A, C.device_id)
            self.assertEqual(DeviceConfig.load(path), A)


if __name__ == '__main__':
    unittest.main()
