from datetime import date, datetime, timedelta, timezone
import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from deskpet_platform.weather import fetch_relay_weather
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.client import Client


class WeatherRelayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 8, tzinfo=timezone.utc).timestamp()
        self.forecast = {
            "utc_offset_seconds": 0,
            "current": {"time": "2026-11-12T07:55", "temperature_2m": 14.1,
                        "weather_code": 0, "is_day": 1},
            "daily": {
                "time": [(date(2026, 11, 12) + timedelta(days=i)).isoformat() for i in range(7)],
                "temperature_2m_min": [7] * 7,
                "temperature_2m_max": [15] * 7,
                "weather_code": [2] * 7,
            },
        }
        self.visited = []

    def provider(self, url, timeout):
        self.assertEqual(timeout, 8)
        self.visited.append(urlsplit(url).hostname)
        if self.visited[-1] == "ipwho.is":
            value = {"success": True, "latitude": 31.2, "longitude": 121.5}
        else:
            self.assertEqual(self.visited[-1], "api.open-meteo.com")
            value = self.forecast
        return BytesIO(json.dumps(value).encode())

    def test_device_location_returns_bounded_current_and_week(self):
        result = fetch_relay_weather({"latitude": 31.2, "longitude": 121.5, "label": "Desk"},
                                     opener=self.provider, now=self.now)
        self.assertEqual(result["city"], "Desk")
        self.assertEqual(self.visited, ["api.open-meteo.com"])
        self.assertEqual((result["degrees"], result["icon"], result["ttl_seconds"]), (14, 0, 5100))
        self.assertEqual(bytes.fromhex(result["days"])[:5], bytes([11, 12, 107, 115, 7]))
        self.assertEqual(len(bytes.fromhex(result["days"])), 35)

    def test_device_location_skips_computer_geolocation(self):
        fetch_relay_weather({"latitude": 1.2, "longitude": 3.4, "label": "Desk"},
                            opener=self.provider, now=self.now)
        self.assertEqual(self.visited, ["api.open-meteo.com"])
        with self.assertRaises(ValueError):
            fetch_relay_weather({"latitude": 1.2, "longitude": 3.4, "label": ""},
                                opener=self.provider, now=self.now)

    def test_missing_device_location_never_uses_computer_ip(self):
        with self.assertRaises(ValueError):
            fetch_relay_weather(opener=self.provider, now=self.now)
        self.assertEqual(self.visited, [])

    def test_stale_or_invalid_weather_is_not_sent(self):
        self.forecast["current"]["time"] = "2026-11-12T06:00"
        with self.assertRaises(ValueError):
            fetch_relay_weather({"latitude": 31.2, "longitude": 121.5}, opener=self.provider, now=self.now)
        self.forecast["current"]["time"] = "2026-11-12T07:55"
        self.forecast["daily"]["time"][1] = "2026-11-20"
        with self.assertRaises(ValueError):
            fetch_relay_weather({"latitude": 31.2, "longitude": 121.5}, opener=self.provider, now=self.now)


class WeatherRelaySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_board_receives_fresh_weather_and_device_location_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.snapshot = {"connection": "connected"}
            session.client = client = SimpleNamespace(
                channel=SimpleNamespace(capabilities={"weather_relay": 1}),
                relay_weather=AsyncMock(return_value={"ok": True}))
            location = {"latitude": 3.0, "longitude": 4.0, "label": "Desk"}
            session.weather_path.write_text(json.dumps(location))
            result = {"degrees": 14, "icon": 0, "ttl_seconds": 1200, "days": "00" * 35}
            with patch("deskpet_platform.mcp_server.fetch_relay_weather", return_value=result) as fetch:
                await session.refresh_weather()
            fetch.assert_called_once_with(location)
            client.relay_weather.assert_awaited_once_with(result)

    async def test_reconnect_resends_weather_before_regular_refresh_is_due(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json", no_token_probe=True)
            session.snapshot = {"connection": "connected"}

            def board():
                return SimpleNamespace(channel=SimpleNamespace(capabilities={"weather_relay": 1}),
                                       relay_weather=AsyncMock(), close=AsyncMock())

            first = session.client = board()
            session.weather_path.write_text(json.dumps({"latitude": 3.0, "longitude": 4.0}))
            result = {"degrees": 14, "icon": 0, "ttl_seconds": 1200, "days": "00" * 35}
            with patch("deskpet_platform.mcp_server.fetch_relay_weather", return_value=result):
                await session.refresh_weather()
                first.relay_weather.assert_awaited_once_with(result)
                self.assertGreater(session.weather_due, 0)
                await session.drop()
                second = session.client = board()
                await session.refresh_weather()
            second.relay_weather.assert_awaited_once_with(result)

    async def test_disconnected_device_never_receives_a_stale_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.snapshot = {"connection": "connected"}
            session.client = client = SimpleNamespace(
                channel=SimpleNamespace(capabilities={"weather_relay": 1}),
                relay_weather=AsyncMock())
            session.weather_path.write_text(json.dumps({"latitude": 1.0, "longitude": 2.0}))
            def disconnected(_location):
                session.client = None
                return {"degrees": 1, "icon": 0, "ttl_seconds": 10, "days": "00" * 35}
            with patch("deskpet_platform.mcp_server.fetch_relay_weather", side_effect=disconnected):
                await session.refresh_weather()
            client.relay_weather.assert_not_awaited()


class WeatherRelayCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_city_sent_only_when_device_advertises_support(self):
        client = Client.__new__(Client)
        client._operation = asyncio.Lock()
        client.channel = SimpleNamespace(capabilities={"weather_relay": 1}, send=AsyncMock())
        snapshot = {"degrees": 14, "city": "Suzhou"}
        await client.relay_weather(snapshot)
        client.channel.send.assert_awaited_with("weather_relay", degrees=14)
        client.channel.capabilities["weather_relay_city"] = 1
        await client.relay_weather(snapshot)
        client.channel.send.assert_awaited_with("weather_relay", degrees=14, city="Suzhou")
        self.assertEqual(snapshot["city"], "Suzhou")


if __name__ == "__main__":
    unittest.main()
