"""Bounded summaries of the board's current weather and token quota caches."""

from .channel import DeviceError
from .quota_projection import SOURCES


BOARD_READOUTS = frozenset(("weather_status", "quota_status"))
WEATHER_CONDITIONS = ("clear", "clear_night", "cloudy", "rain", "snow", "fog",
                      "thunderstorm", "partly_cloudy", "unknown")
QUOTA_STATUSES = ("ok", "stale", "clock_pending", "unavailable")


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _weather(reply, status):
    if status in ("stale", "pending", "unavailable"):
        return {"status": status}
    degrees = reply.get("degrees_c")
    condition = reply.get("condition")
    city = reply.get("city")
    if (status != "ok" or not _integer(degrees, -100, 100) or condition not in WEATHER_CONDITIONS
            or ("city" in reply and (not isinstance(city, str) or not city.isprintable()
                                    or not 0 < len(city.encode("utf-8")) <= 47))):
        raise DeviceError("invalid_weather_status_reply")
    result = {"status": status, "degrees_c": degrees, "condition": condition}
    if city is not None:
        result["city"] = city
    return result


def _quota_windows(reply):
    if not isinstance(reply, list) or not 1 <= len(reply) <= 2:
        raise DeviceError("invalid_quota_status_reply")
    windows = []
    for window in reply:
        if not isinstance(window, dict):
            raise DeviceError("invalid_quota_status_reply")
        remaining = window.get("remaining_percent")
        minutes = window.get("window_minutes")
        reset = window.get("reset_at")
        if (not _integer(remaining, 0, 100) or not _integer(minutes, 1, 525600)
                or "reset_at" not in window or (reset is not None and not _integer(reset, 1700000000, 0xFFFFFFFF))
                or any(period["window_minutes"] == minutes for period in windows)):
            raise DeviceError("invalid_quota_status_reply")
        windows.append({"remaining_percent": remaining, "window_minutes": minutes, "reset_at": reset})
    return windows


def _quota(reply, status):
    if status == "stale" and "sources" not in reply:
        return {"status": "stale"}
    sources = reply.get("sources")
    if status not in QUOTA_STATUSES or not isinstance(sources, list) or len(sources) != len(SOURCES):
        raise DeviceError("invalid_quota_status_reply")
    parsed = []
    for raw, name in zip(sources, SOURCES):
        if not isinstance(raw, dict) or raw.get("source") != name or raw.get("status") not in QUOTA_STATUSES:
            raise DeviceError("invalid_quota_status_reply")
        source = {"source": name, "status": raw["status"]}
        if raw["status"] == "ok":
            source["windows"] = _quota_windows(raw.get("windows"))
        parsed.append(source)
    source_statuses = {source["status"] for source in parsed}
    if status != next(known for known in QUOTA_STATUSES if known in source_statuses):
        raise DeviceError("invalid_quota_status_reply")
    return {"status": status, "sources": parsed}


def board_readout(name, reply):
    status = reply.get("status")
    if name == "weather_status":
        return _weather(reply, status)
    if name == "quota_status":
        return _quota(reply, status)
    raise ValueError("invalid board readout")
