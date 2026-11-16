import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from deskpet_platform.mcp_server import BoardSession, status_view
from deskpet_platform.quota_capture import QuotaStore


class QuotaDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_claude_quota_reaches_board_even_without_speed_probe(self):
        class Channel:
            capabilities = {"quota_sources": 1, "token_stats": 1}

        class Board:
            channel = Channel()
            def __init__(self):
                self.received = asyncio.Event()
                self.snapshots = []
            async def update_quota(self, reading):
                self.snapshots.append(reading)
                self.received.set()

        with tempfile.TemporaryDirectory(prefix="deskpet-quota-display-") as directory:
            root = Path(directory)
            store = QuotaStore(root / "quota.sqlite3")
            now = int(time.time())
            store.record("claude_code", {"rate_limits": {
                "five_hour": {"used_percentage": 20, "resets_at": now + 300},
                "seven_day": {"used_percentage": 90, "resets_at": now + 3000}}}, time.time())
            session = BoardSession(root / "device.json", quota_store=store, no_token_probe=True)
            session.client = board = Board()
            session.snapshot["quota_source"] = "claude_code"
            session.codex_allowance = {"quotas": [[99, 300, now + 300]], "credits": "100", "reset_cards": 2}
            task = asyncio.create_task(session.poll_quota_display())
            try:
                await asyncio.wait_for(board.received.wait(), 2)
                self.assertEqual(board.snapshots, [{"source": "claude_code", "quotas": [
                    [80, 300, now + 300], [10, 10080, now + 3000]]}])
                self.assertIsNone(session.probe_collector)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_antigravity_refresh_is_shared_even_if_the_cli_returns_no_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "quota.sqlite3"
            first, second = QuotaStore(cache), QuotaStore(cache)
            first.set_antigravity_refresh(True)
            with patch("deskpet_platform.antigravity_quota.fetch_usage_buckets", return_value={}) as fetch:
                await asyncio.gather(asyncio.to_thread(first.refresh_antigravity),
                                     asyncio.to_thread(second.refresh_antigravity))
                second.refresh_antigravity()
                fetch.assert_called_once()

    def test_antigravity_claim_rejects_an_earlier_timestamp_from_a_concurrent_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QuotaStore(Path(directory) / "quota.sqlite3")
            store.set_antigravity_refresh(True)
            with patch("deskpet_platform.quota_capture.time.time", side_effect=[1000, 999.99, 1120]), patch(
                    "deskpet_platform.antigravity_quota.fetch_usage_buckets", return_value={}) as fetch:
                store.refresh_antigravity()
                store.refresh_antigravity()
                fetch.assert_called_once()
                store.refresh_antigravity()
                self.assertEqual(fetch.call_count, 2)

    async def test_disabling_antigravity_refresh_stops_the_cli_in_other_workers(self):
        from deskpet_platform.mcp_server import create_server
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            root = Path(directory)
            server = create_server(root / "device.json", no_token_probe=True)
            worker = QuotaStore(root / "agent-quota.sqlite3")
            with patch("deskpet_platform.antigravity_quota.fetch_usage_buckets", return_value={}) as fetch:
                worker.refresh_antigravity()
                fetch.assert_not_called()
                await server.call_tool("desk_set_agent_quota", {"source": "antigravity", "enabled": True})
                worker.refresh_antigravity()
                fetch.assert_called_once()
                await server.call_tool("desk_set_agent_quota", {"source": "antigravity", "enabled": False})
                with patch("deskpet_platform.quota_capture.time.time", return_value=time.time() + 180):
                    worker.refresh_antigravity()
                fetch.assert_called_once()

    async def test_idle_background_session_does_not_start_antigravity_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = BoardSession(root / "device.json", background=True, no_token_probe=True)
            session.snapshot["quota_source"] = "antigravity_gemini"
            with patch.object(session.quota_store, "refresh_antigravity") as refresh:
                task = asyncio.create_task(session.poll_antigravity_quota())
                try:
                    await asyncio.sleep(0)
                    refresh.assert_not_called()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_missing_agent_reading_publishes_empty_selection(self):
        class Channel:
            capabilities = {"quota_sources": 1}
        class Board:
            channel = Channel()
            def __init__(self): self.received = asyncio.Event(); self.snapshots = []
            async def update_quota(self, reading): self.snapshots.append(reading); self.received.set()
        with tempfile.TemporaryDirectory(prefix="deskpet-quota-display-") as directory:
            root = Path(directory)
            session = BoardSession(root / "device.json", no_token_probe=True)
            session.client = board = Board()
            session.snapshot["quota_source"] = "antigravity_third_party"
            task = asyncio.create_task(session.poll_quota_display())
            try:
                await asyncio.wait_for(board.received.wait(), 2)
                self.assertEqual(board.snapshots, [{"source": "antigravity_third_party", "quotas": []}])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_old_codex_account_reading_is_not_kept_alive_by_the_screen_sender(self):
        class Channel:
            capabilities = {"quota_sources": 1}
        class Board:
            channel = Channel()
            def __init__(self): self.received = asyncio.Event(); self.snapshots = []
            async def update_quota(self, reading): self.snapshots.append(reading); self.received.set()
        with tempfile.TemporaryDirectory(prefix="deskpet-quota-display-") as directory:
            session = BoardSession(Path(directory) / "device.json", no_token_probe=True)
            session.client = board = Board()
            session.snapshot["quota_source"] = "codex"
            session.codex_allowance = session.codex_live_allowance = {
                "quotas": [[80, 10080, int(time.time()) + 3600]], "credits": "10"}
            session.codex_live_observed_at = time.time() - 601
            task = asyncio.create_task(session.poll_quota_display())
            try:
                await asyncio.wait_for(board.received.wait(), 2)
                self.assertEqual(board.snapshots, [{"source": "codex", "quotas": []}])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_status_maps_only_valid_advertised_device_selection(self):
        capabilities = {"quota_sources": 1}
        self.assertEqual(status_view({"quota_source": 1}, capabilities)["quota_source"], "claude_code")
        self.assertEqual(status_view({"quota_source": 3}, capabilities)["quota_source"], "antigravity_third_party")
        for value in (True, -1, 4, "1", None):
            self.assertNotIn("quota_source", status_view({"quota_source": value}, capabilities))
        self.assertNotIn("quota_source", status_view({"quota_source": 1}, {}))


if __name__ == "__main__":
    unittest.main()
