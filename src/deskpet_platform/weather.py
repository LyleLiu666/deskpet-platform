"""Fetch the current conditions and seven-day forecast for a configured point."""
from datetime import date, datetime, timedelta, timezone
import json
import math
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def _weather_icon(code, *, day=True, forecast=False):
    if type(code) is not int:
        return None
    if code == 0:
        return 0 if day else 1
    if code == 1:
        return 0 if forecast else 2
    if code == 2:
        return 7 if forecast else 2
    if code == 3:
        return 2
    if code in (45, 48):
        return 5
    if code in (71, 73, 75, 77, 85, 86):
        return 4
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return 3
    if code in (95, 96, 99):
        return 6
    return None


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rounded(value):
    return math.floor(value + .5) if value >= 0 else math.ceil(value - .5)


def fetch_relay_weather(location=None, *, opener=urlopen, now=None):
    """Fetch weather for a location supplied by the device or explicitly configured for it."""
    now = time.time() if now is None else now
    try:
        if location is None:
            raise ValueError()
        latitude, longitude = location["latitude"], location["longitude"]
        if (not _finite_number(latitude) or not _finite_number(longitude) or
                not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError()
        if "label" in location:
            label = location["label"]
            if not isinstance(label, str) or not 1 <= len(label) <= 24 or not label.isascii() or not label.isprintable():
                raise ValueError()
        query = urlencode({
            "latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,weather_code,is_day",
            "daily": "temperature_2m_min,temperature_2m_max,weather_code",
        })
        with opener("https://api.open-meteo.com/v1/forecast?" + query, timeout=8) as response:
            data = json.loads(response.read(16385))
        current, daily, offset = data["current"], data["daily"], data["utc_offset_seconds"]
        if type(offset) is not int or not -50400 <= offset <= 50400:
            raise ValueError()
        observed = datetime.strptime(current["time"], "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone(timedelta(seconds=offset)))
        age = now - observed.timestamp()
        temperature, daylight = current["temperature_2m"], current["is_day"]
        if (not _finite_number(temperature) or not -100 <= temperature <= 100 or
                type(daylight) is not int or daylight not in (0, 1) or age >= 5400 or age < -120):
            raise ValueError()
        icon = _weather_icon(current["weather_code"], day=daylight == 1)
        if icon is None:
            raise ValueError()
        fields = (daily["time"], daily["temperature_2m_min"], daily["temperature_2m_max"], daily["weather_code"])
        if any(not isinstance(field, list) or len(field) != 7 for field in fields):
            raise ValueError()
        days = bytearray()
        for index, (text, low, high, code) in enumerate(zip(*fields)):
            day = date.fromisoformat(text)
            if day != observed.date() + timedelta(days=index):
                raise ValueError()
            kind = _weather_icon(code, forecast=True)
            valid = (_finite_number(low) and _finite_number(high) and -100 <= low <= high <= 100 and kind is not None)
            days.extend((day.month, day.day, _rounded(low) + 100 if valid else 100,
                         _rounded(high) + 100 if valid else 100, kind if valid else 255))
        ttl = int(5400 - max(0, age))
        if ttl < 1:
            raise ValueError()
        result = {"degrees": _rounded(temperature), "icon": icon, "ttl_seconds": ttl, "days": days.hex()}
        if "label" in location:
            result["city"] = location["label"]
        return result
    except (OSError, KeyError, IndexError, TypeError, OverflowError, ValueError):
        raise ValueError("无法取得有效的天气数据。") from None


def fetch_weather(latitude, longitude, label, *, opener=urlopen):
    if (not math.isfinite(latitude) or not math.isfinite(longitude) or
            not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or
            not 1 <= len(label) <= 24 or not label.isascii() or not label.isprintable()):
        raise ValueError("请提供有效的经纬度和 1 至 24 字符的英文地点名。")
    query = urlencode({
        "latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,weather_code",
        "daily": "weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max",
    })
    try:
        with opener("https://api.open-meteo.com/v1/forecast?" + query, timeout=8) as response:
            data = json.loads(response.read(65536))
        current, daily = data["current"], data["daily"]
        days = []
        for index in range(7):
            day = date.fromisoformat(daily["time"][index])
            low, high = (round(daily[field][index]) for field in ("temperature_2m_min", "temperature_2m_max"))
            code, rain = (daily[field][index] for field in ("weather_code", "precipitation_probability_max"))
            if (not -100 <= low <= high <= 100 or type(code) is not int or not 0 <= code <= 99 or
                    type(rain) is not int or not 0 <= rain <= 100):
                raise ValueError()
            days.append([day.month, day.day, low, high, code, rain])
        temperature, code = round(current["temperature_2m"]), current["weather_code"]
        if not -100 <= temperature <= 100 or type(code) is not int or not 0 <= code <= 99:
            raise ValueError()
    except (OSError, KeyError, IndexError, TypeError, OverflowError, ValueError):
        raise ValueError("无法取得完整的天气数据。") from None
    return {"label": label, "updated": int(time.time()), "temperature": temperature,
            "condition": code, "days": days}
