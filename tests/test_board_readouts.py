import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from deskpet_platform.channel import DeviceError
from deskpet_platform.client import Client
from deskpet_platform.quota_projection import SOURCES


class BoardReadoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client.__new__(Client)
        self.client.link = SimpleNamespace(wireless=True)
        self.client._operation = asyncio.Lock()
        self.client.channel = SimpleNamespace(capabilities={"weather_status": 1, "quota_status": 1},
                                              send=AsyncMock())

    async def test_query_requires_negotiated_capability_and_paired_link(self):
        self.client.channel.capabilities = {"weather_relay": 1, "quota_multi": 1}
        with self.assertRaisesRegex(DeviceError, "weather_status_not_supported"):
            await self.client.read_board_status("weather_status")
        self.client.channel.capabilities["quota_status"] = 1
        self.client.link.wireless = False
        with self.assertRaisesRegex(DeviceError, "requires_paired_wifi"):
            await self.client.read_board_status("quota_status")
        self.client.channel.send.assert_not_awaited()

    async def test_weather_preserves_fresh_zero_and_omits_private_extra_fields(self):
        self.client.channel.send.return_value = {"status": "ok", "degrees_c": 0, "condition": "snow",
                                                 "city": "上海", "latitude": 30, "token": "hidden"}
        self.assertEqual(await self.client.read_board_status("weather_status"),
                         {"status": "ok", "degrees_c": 0, "condition": "snow", "city": "上海"})
        self.client.channel.send.assert_awaited_once_with("weather_status")
        self.client.channel.send.return_value["status"] = "stale"
        self.assertEqual(await self.client.read_board_status("weather_status"), {"status": "stale"})
        self.client.channel.send.return_value.update(status="ok", degrees_c=True)
        with self.assertRaisesRegex(DeviceError, "invalid_weather_status_reply"):
            await self.client.read_board_status("weather_status")

    async def test_quota_filters_old_and_sensitive_values_and_rejects_malformed_windows(self):
        candidates = [{"source": name, "status": "unavailable"} for name in SOURCES]
        window = {"remaining_percent": 0, "window_minutes": 300, "reset_at": None, "private": True}
        candidates[0].update(status="ok", windows=[window])
        candidates[1].update(status="stale", windows=[window])
        self.client.channel.send.return_value = {"status": "ok", "sources": candidates, "credentials": "secret"}
        expected = [dict(row) for row in candidates]
        expected[0]["windows"] = [{key: value for key, value in window.items() if key != "private"}]
        del expected[1]["windows"]
        self.assertEqual(await self.client.read_board_status("quota_status"), {"status": "ok", "sources": expected})
        self.client.channel.send.assert_awaited_once_with("quota_status")
        candidates[0]["windows"].append(window)
        with self.assertRaisesRegex(DeviceError, "invalid_quota_status_reply"):
            await self.client.read_board_status("quota_status")
        self.client.channel.send.return_value = {"status": "stale"}
        self.assertEqual(await self.client.read_board_status("quota_status"), {"status": "stale"})
