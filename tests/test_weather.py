from datetime import date, timedelta
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from deskpet_platform.weather import fetch_weather
from test_role_pack import ROOT


class WeatherTests(unittest.TestCase):
    def test_seven_day_snapshot_uses_provider_timezone_and_validates_bounds(self):
        daily = {
            "time": [(date(2026, 11, 30) + timedelta(days=i)).isoformat() for i in range(7)],
            "temperature_2m_min": [4.2] * 7, "temperature_2m_max": [14.8] * 7,
            "weather_code": [2] * 7, "precipitation_probability_max": [30] * 7,
        }

        def provider(url, timeout):
            self.assertEqual(urlsplit(url).hostname, "api.open-meteo.com")
            self.assertEqual(parse_qs(urlsplit(url).query)["timezone"], ["auto"])
            self.assertEqual(timeout, 8)
            return BytesIO(json.dumps({"current": {"temperature_2m": 12.4, "weather_code": 3},
                                       "daily": daily}).encode())

        reading = fetch_weather(31.2, 121.5, "Fixture", opener=provider)
        self.assertEqual(reading["temperature"], 12)
        self.assertEqual(reading["days"][1], [12, 1, 4, 15, 2, 30])
        self.assertEqual(len(reading["days"]), 7)
        daily["weather_code"][3] = None
        with self.assertRaises(ValueError):
            fetch_weather(31.2, 121.5, "Fixture", opener=provider)
        with self.assertRaises(ValueError):
            fetch_weather(91, 0, "Fixture", opener=provider)
