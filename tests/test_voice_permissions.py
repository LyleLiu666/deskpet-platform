import json
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.voice_permissions import VOICE_TOOLS, VoicePermissions


class VoicePermissionsTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'device.json'
        self.config=DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32)))
        self.config.save(self.path)

    def test_default_deny_explicit_update_and_restart(self):
        store=VoicePermissions(self.path)
        self.assertEqual(store.enabled(),frozenset())
        store.update(['device_status','list_role_actions'])
        self.assertEqual(VoicePermissions(self.path).enabled(),{'device_status','list_role_actions'})
        store.update([]);self.assertEqual(VoicePermissions(self.path).enabled(),frozenset())

    def test_wrong_device_or_corrupt_file_never_inherits_authorization(self):
        store=VoicePermissions(self.path);store.update(['device_status'])
        DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(reversed(range(32)))).save(self.path)
        self.assertEqual(store.enabled(),frozenset())
        store.path.write_text('{broken',encoding='utf-8')
        self.assertEqual(store.enabled(),frozenset())
        self.assertEqual(store.inspect()[1],'invalid')

    def test_invalid_request_and_failed_save_preserve_previous_settings(self):
        store=VoicePermissions(self.path);store.update(['device_status'])
        for value in (['music_delete'],['device_status','device_status'],['bogus'],None,'device_status'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):store.update(value)
        with patch('deskpet_platform.voice_permissions.os.replace',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):store.update(['list_role_actions'])
        self.assertEqual(store.enabled(),{'device_status'})
        self.assertEqual(len(list(self.path.parent.glob('.voice-tools-*'))),0)

    def test_modified_or_extra_fields_fail_closed(self):
        store=VoicePermissions(self.path);store.update(['device_status'])
        data=json.loads(store.path.read_text());data['enabled'].append('music_delete')
        store.path.write_text(json.dumps(data),encoding='utf-8')
        self.assertEqual(store.enabled(),frozenset())
        self.assertEqual(store.inspect()[1],'invalid')

    def test_boolean_version_is_not_a_valid_version(self):
        store=VoicePermissions(self.path);store.update(['device_status'])
        data=json.loads(store.path.read_text());data['version']=True
        store.path.write_text(json.dumps(data),encoding='utf-8')
        self.assertEqual(store.enabled(),frozenset());self.assertEqual(store.inspect()[1],'invalid')

    @unittest.skipIf(os.name=='nt','POSIX FIFO and symlink test')
    def test_special_files_and_symlinks_fail_closed(self):
        store=VoicePermissions(self.path);os.mkfifo(store.path)
        self.assertEqual(store.inspect()[1],'invalid')
        store.path.unlink();store.path.symlink_to(self.path)
        self.assertEqual(store.inspect()[1],'invalid')

    def test_unpaired_device_cannot_enable_permissions(self):
        self.path.unlink();store=VoicePermissions(self.path)
        self.assertEqual(store.enabled(),frozenset())
        with self.assertRaises(ValueError):store.update(['device_status'])


class VoicePermissionsMCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_permission_update_waits_for_inflight_authorized_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json';DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            first=VoicePermissions(path);second=VoicePermissions(path)
            first.update(['list_role_actions']);holding=asyncio.Event();release=asyncio.Event()
            async def action():
                async with first.lock():
                    self.assertIn('list_role_actions',first.enabled());holding.set();await release.wait()
            async def revoke():
                async with second.lock():second.update([])
            active=asyncio.create_task(action());await holding.wait()
            revoked=asyncio.create_task(revoke());await asyncio.sleep(.1)
            self.assertFalse(revoked.done())
            release.set();await asyncio.wait_for(asyncio.gather(active,revoked),1)
            self.assertEqual(first.enabled(),frozenset())

    async def test_tool_can_inspect_set_and_revoke_paired_device_permissions(self):
        from deskpet_platform.mcp_server import create_server
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'device.json'
            DeviceConfig('aa:bb:cc:dd:ee:ff',bytes(range(32))).save(path)
            server=create_server(path,no_token_probe=True)
            tool = next(tool for tool in await server.list_tools() if tool.name == 'desk_voice_tools')
            enabled_schema = tool.inputSchema['properties']['enabled']
            item_schema = next(choice['items'] for choice in enabled_schema['anyOf'] if choice['type'] == 'array')
            self.assertEqual(set(item_schema['enum']), set(VOICE_TOOLS))
            result=(await server.call_tool('desk_voice_tools',{}))[1]
            self.assertEqual(result['status'],'ok');self.assertEqual(result['enabled'],[])
            result=(await server.call_tool('desk_voice_tools',{'enabled':['device_status']}))[1]
            self.assertEqual(result['enabled'],['device_status'])
            result = (await server.call_tool('desk_voice_tools', {'enabled': ['weather_status', 'quota_status']}))[1]
            self.assertEqual(result['enabled'], ['quota_status', 'weather_status'])
            result = (await server.call_tool('desk_voice_tools', {'enabled': sorted(VOICE_TOOLS)}))[1]
            self.assertEqual(result['enabled'], sorted(VOICE_TOOLS))
            result=(await server.call_tool('desk_voice_tools',{'enabled':[]}))[1]
            self.assertEqual(result['enabled'],[])
