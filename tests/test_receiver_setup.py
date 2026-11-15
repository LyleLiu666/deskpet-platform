from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.receiver_setup import add_usb_receiver


class ReceiverSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_usb_pairing_verifies_before_adding_independent_receiver(self):
        with TemporaryDirectory() as directory:
            shared=Path(directory)/'device.json'
            original=DeviceConfig('02:00:00:00:00:01',bytes(range(32)))
            device=DeviceConfig('02:00:00:00:00:02',bytes(range(32)),'192.168.1.2')
            original.save(shared)
            class Port:
                checked=0
                def verify(self):self.checked+=1
            port=Port()
            class Board:
                device_id=None
                def __init__(self,config):self.config=config
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def status(self):return {'ok':True}
            with patch('deskpet_platform.receiver_setup.UsbPort.register',return_value=port),\
                 patch('deskpet_platform.receiver_setup.probe_usb',return_value=device),\
                 patch('deskpet_platform.receiver_setup.Client',Board):
                result=await add_usb_receiver(shared,'/dev/cu.test',device.identity)
            self.assertGreaterEqual(port.checked,2)
            self.assertEqual(DeviceConfig.load(shared),original)
            registry=DeviceRegistry(shared);self.assertEqual(len(registry.list()),1)
            receiver=Path(result['config'])
            self.assertEqual(DeviceConfig.load(receiver),device)
            self.assertEqual({p.resolve() for p in registry.notification_paths()},{shared.resolve(),receiver.resolve()})

    async def test_failed_authentication_never_creates_receiver(self):
        with TemporaryDirectory() as directory:
            shared=Path(directory)/'device.json';DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(shared)
            device=DeviceConfig('02:00:00:00:00:02',bytes(range(32)),'192.168.1.2')
            class Port:
                def verify(self):pass
            class Board:
                def __init__(self,config):pass
                async def __aenter__(self):raise OSError('authentication failed')
                async def __aexit__(self,*args):pass
            with patch('deskpet_platform.receiver_setup.UsbPort.register',return_value=Port()),\
                 patch('deskpet_platform.receiver_setup.probe_usb',return_value=device),\
                 patch('deskpet_platform.receiver_setup.Client',Board):
                with self.assertRaises(OSError):await add_usb_receiver(shared,'/dev/cu.test',device.identity)
            self.assertFalse((shared.parent/'receivers').exists())

    async def test_offline_device_is_not_registered(self):
        with TemporaryDirectory() as directory:
            shared=Path(directory)/'device.json';DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(shared)
            device=DeviceConfig('02:00:00:00:00:02',bytes(range(32)))
            class Port:
                def verify(self):pass
            with patch('deskpet_platform.receiver_setup.UsbPort.register',return_value=Port()),\
                 patch('deskpet_platform.receiver_setup.probe_usb',return_value=device),\
                 patch('deskpet_platform.receiver_setup.Client') as client:
                with self.assertRaises(ValueError):await add_usb_receiver(shared,'/dev/cu.test',device.identity)
                client.assert_not_called()
            self.assertFalse((shared.parent/'receivers').exists())


if __name__=='__main__':unittest.main()
