from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deskpet_platform.device_registry import DeviceRegistry
from deskpet_platform.mcp_server import BoardSession, create_server
from deskpet_platform.notifications import NotificationStore
from deskpet_platform.pairing import DeviceConfig


class SharedNotificationSourceTests(unittest.TestCase):
    def test_independent_server_uses_existing_notifications_and_quota(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);shared=root/'shared'/'device.json'
            DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(shared)
            registry=DeviceRegistry(shared)
            receiver=registry.add_receiver(DeviceConfig('02:00:00:00:00:02',bytes(range(32))))
            source=NotificationStore(shared.parent/'notifications.sqlite3')
            source.add_agent_event('codex','session','question','response','Synthetic')
            captured=[]
            def make_session(*args,**kwargs):
                board=BoardSession(*args,**kwargs);captured.append(board);return board
            with patch('deskpet_platform.mcp_server.BoardSession',side_effect=make_session):
                create_server(receiver,no_token_probe=True,shared_config_path=shared)
            board=captured[0]
            self.assertEqual(board.notifications.source.path.resolve(),source.path.resolve())
            self.assertEqual(board.quota_store.path.resolve(),(shared.parent/'agent-quota.sqlite3').resolve())
            board.notifications.sync()
            self.assertEqual(board.notifications.home_summary()['response_count'],1)
            identity=board.notifications.list()[0]['id'];board.notifications.mark_seen(identity)
            self.assertEqual(source.unseen_count(),1)
            self.assertEqual(len(registry.list()),1)

    def test_shared_configuration_must_exist_and_be_private(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises((ValueError,FileNotFoundError)):
                create_server(root/'device.json',no_token_probe=True,shared_config_path=root/'missing.json')


class SharedHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabling_notifications_keeps_shared_hook_as_canonical(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);shared=root/'shared'/'device.json'
            DeviceConfig('02:00:00:00:00:01',bytes(range(32))).save(shared)
            registry=DeviceRegistry(shared)
            receiver=registry.add_receiver(DeviceConfig('02:00:00:00:00:02',bytes(range(32))))
            server=create_server(receiver,no_token_probe=True,shared_config_path=shared)
            tool=server._tool_manager._tools['desk_enable_codex_notifications']
            with patch('deskpet_platform.mcp_server.configure_codex_hooks',return_value=False) as configure:
                await tool.fn()
            _hook_file,command,previous=configure.call_args.args
            self.assertIn(str(shared),command)
            self.assertTrue(any(str(receiver) in old for old in previous))
            self.assertFalse(any(str(shared) in old for old in previous))


if __name__=='__main__':unittest.main()
