import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import AsyncMock, Mock, patch

from deskpet_platform.client import Client
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.notifications import NotificationStore


class AttentionSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_book_attention_refreshes_during_a_history_scan(self):
        with TemporaryDirectory() as directory:
            started = asyncio.Event()
            finish = Event()
            loop = asyncio.get_running_loop()

            def collect(_now):
                loop.call_soon_threadsafe(started.set)
                finish.wait(5)

            store = Mock(collect=collect)
            session = BoardSession(Path(directory) / "device.json", no_token_probe=True, analytics_store=store)
            client = Mock()
            client.channel.capabilities = {"agent_analytics": 1}
            client.channel.online, client.channel.epoch = True, "board"
            client.channel.send = AsyncMock()
            client.status = AsyncMock(return_value={})
            session.client = client
            session.recover_wifi = AsyncMock()
            session.sync_computer_preferences = AsyncMock()
            session.record_status = Mock()
            history = asyncio.create_task(session.analytics.step(client, quota_store=session.quota_store))
            try:
                await asyncio.wait_for(started.wait(), 3)
                client.channel.send.reset_mock()
                session.notifications.add_agent_event("codex", "running", "ask", "response", "Need input")
                with patch("deskpet_platform.mcp_server.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await session.poll()
                client.channel.send.assert_awaited_once_with(
                    "analytics_attention", rows=session.notifications.analytics_attention())
            finally:
                finish.set()
                history.cancel()
                await asyncio.gather(history, return_exceptions=True)

    async def test_client_only_sends_to_supported_device(self):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = type('Channel', (), {})()
        client.channel.capabilities = {'attention_summary': 1}
        client.channel.send = AsyncMock(return_value={'ok': True})
        await client.set_attention_summary(3)
        client.channel.send.assert_awaited_once_with('attention_summary', count=3)
        client.channel.capabilities['attention_source'] = 1
        await client.set_attention_summary(2,source=1)
        client.channel.send.assert_awaited_with('attention_summary', count=2,source=1)
        await client.set_attention_summary(0,source=1)
        client.channel.send.assert_awaited_with('attention_summary', count=0,source=0)
        with self.assertRaises(ValueError): await client.set_attention_summary(2,source=7)
        client.channel.capabilities = {}
        await client.set_attention_summary(5)
        self.assertEqual(client.channel.send.await_count, 3)

    async def test_unanswered_is_distinct_from_unread_and_refreshes(self):
        with TemporaryDirectory() as directory:
            session = BoardSession(Path(directory)/'device.json', no_token_probe=True)
            client = type('Client', (), {})()
            client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1}})()
            client.set_attention_summary = AsyncMock()
            session.notifications.add_agent_event('codex', 'session', 'older', 'response', 'Earlier question')
            question = session.notifications.add_agent_event('codex', 'session', 'question', 'response', 'Question')
            session.notifications.mark_seen(question)
            await session.refresh_attention_summary(client, now=0)
            client.set_attention_summary.assert_awaited_once_with(1)
            await session.refresh_attention_summary(client, now=1)
            self.assertEqual(client.set_attention_summary.await_count, 1)
            await session.refresh_attention_summary(client, now=5)
            self.assertEqual(client.set_attention_summary.await_count, 2)
            session.notifications.resolve_response(question)
            await session.refresh_attention_summary(client, now=6)
            client.set_attention_summary.assert_awaited_with(0)
            client.channel.capabilities = {}
            await session.refresh_attention_summary(client, now=20)
            self.assertEqual(client.set_attention_summary.await_count, 3)

    async def test_source_changes_refresh_even_when_count_is_unchanged(self):
        with TemporaryDirectory() as directory:
            session = BoardSession(Path(directory)/'device.json', no_token_probe=True)
            client = type('Client', (), {})()
            client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1, 'attention_source': 1}})()
            client.set_attention_summary = AsyncMock()
            first=session.notifications.add_agent_event('codex','session','q1','response','Question')
            await session.refresh_attention_summary(client,now=0)
            client.set_attention_summary.assert_awaited_with(1,source=1)
            session.notifications.resolve_response(first)
            session.notifications.add_agent_event('claude_code','session','q2','response','Question')
            await session.refresh_attention_summary(client,now=1)
            client.set_attention_summary.assert_awaited_with(1,source=2)
            session.notifications.add_agent_event('opencode','session','q3','response','Question')
            await session.refresh_attention_summary(client,now=2)
            client.set_attention_summary.assert_awaited_with(2,source=6)

    async def test_failed_send_is_retried_and_new_client_gets_current_state(self):
        with TemporaryDirectory() as directory:
            session = BoardSession(Path(directory)/'device.json', no_token_probe=True)
            def make_client():
                client = type('Client', (), {})()
                client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1}})()
                client.set_attention_summary = AsyncMock()
                return client
            client = make_client();client.set_attention_summary.side_effect = [OSError('offline'), None]
            with self.assertRaises(OSError): await session.refresh_attention_summary(client, now=0)
            await session.refresh_attention_summary(client, now=1)
            self.assertEqual(client.set_attention_summary.await_count, 2)
            replacement = make_client()
            await session.refresh_attention_summary(replacement, now=2)
            replacement.set_attention_summary.assert_awaited_once_with(0)

    async def test_unavailable_store_does_not_refresh_stale_summary(self):
        with TemporaryDirectory() as directory:
            session = BoardSession(Path(directory)/'device.json', no_token_probe=True)
            client = type('Client', (), {})()
            client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1}})()
            client.set_attention_summary = AsyncMock()
            session.notifications.home_summary = unittest.mock.Mock(side_effect=OSError('unavailable'))
            await session.refresh_attention_summary(client, now=0)
            client.set_attention_summary.assert_not_awaited()
            self.assertIn('notification_error', session.snapshot)

    async def test_shared_source_resolution_reaches_device_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = NotificationStore(root/'shared'/'notifications.sqlite3')
            session = BoardSession(root/'device'/'config.json', notification_source=source, no_token_probe=True)
            client = type('Client', (), {})()
            client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1}})()
            client.set_attention_summary = AsyncMock()
            identity = source.add_agent_event('codex', 'session', 'question', 'response', 'Question')
            session.notifications.sync()
            await session.refresh_attention_summary(client, now=0)
            client.set_attention_summary.assert_awaited_with(1)
            source.resolve_response(identity)
            session.notifications.sync()
            await session.refresh_attention_summary(client, now=1)
            client.set_attention_summary.assert_awaited_with(0)

    async def test_poll_sends_only_after_notification_sync_succeeds(self):
        for sync_error in (None, OSError('inbox unavailable')):
            with self.subTest(sync_error=sync_error), TemporaryDirectory() as directory:
                session = BoardSession(Path(directory)/'device.json', no_token_probe=True)
                client = type('Client', (), {})()
                client.channel = type('Channel', (), {'capabilities': {'attention_summary': 1}})()
                client.status = AsyncMock(return_value={})
                session.client = client
                session.recover_wifi = AsyncMock()
                session.sync_computer_preferences = AsyncMock()
                session.record_status = Mock()
                session.notifications.sync = Mock(side_effect=sync_error)
                session.refresh_attention_summary = AsyncMock()
                with patch('deskpet_platform.mcp_server.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError): await session.poll()
                if sync_error:
                    session.refresh_attention_summary.assert_not_awaited()
                else:
                    session.refresh_attention_summary.assert_awaited_once_with(client)


if __name__ == '__main__': unittest.main()
