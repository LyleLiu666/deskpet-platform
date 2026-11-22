"""Validate and encode the explicit patch sent to an onboard assistant."""

from ipaddress import ip_address, ip_network
import json
import re
from urllib.parse import urlsplit

from .channel import DeviceError
from .voice_names import validate_voice_name


MAX_AGENT_CONFIG_BYTES = 4096
# 600 bytes encode to 800 Base64 characters, leaving room for the P1 envelope.
AGENT_CONFIG_CHUNK_BYTES = 600
AGENT_MODES = ("platform", "onboard", "auto")
AGENT_SERVICE_GROUPS = ("chat", "asr", "tts")
_PRIVATE_NETWORKS = tuple(ip_network(network) for network in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
_GROUP_FIELDS = {
    "chat": {"url", "token", "model", "tool_format"},
    "asr": {"url", "token", "model"},
    "tts": {"url", "token", "model", "voice"},
    "persona": {"prompt", "user_name", "device_name"},
}
_ROOT_FIELDS = {"mode", "wake_enabled", *_GROUP_FIELDS}


class AgentConfigUnconfirmed(DeviceError):
    """The commit may have reached the device. Resolve it by querying status."""

    def __init__(self, tx):
        self.tx = tx
        super().__init__("agent_config_commit_unconfirmed")


def valid_agent_config_tx(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value) is not None


def _valid_text(value, maximum, *, extra_whitespace=""):
    return (isinstance(value, str) and len(value.encode("utf-8")) <= maximum and
            all(char in extra_whitespace or 32 <= ord(char) < 127 or ord(char) >= 160 for char in value))


def _valid_url(value):
    if not isinstance(value, str) or len(value) > 256 or any(not 33 <= ord(char) < 127 for char in value):
        return False
    if not value:
        return True
    if not value.startswith(("https://", "http://")) or any(char in value for char in "\\?#"):
        return False
    parsed = urlsplit(value)
    host = parsed.hostname
    if (not host or len(host) > 253 or "%" in parsed.netloc or parsed.username is not None or
            parsed.port == 0 or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)):
        return False
    port = parsed.netloc.partition("]")[2] if parsed.netloc.startswith("[") else parsed.netloc[len(host):]
    if port and re.fullmatch(r":[0-9]{1,5}", port) is None:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return (parsed.scheme == "https" and not parsed.netloc.startswith("[") and
                all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) is not None
                    for part in host.split(".")))
    return parsed.scheme == "https" or any(address in network for network in _PRIVATE_NETWORKS)


def _validate_group(group, values):
    if not isinstance(values, dict) or not values or not values.keys() <= _GROUP_FIELDS[group]:
        raise ValueError
    for field, value in values.items():
        if field == "url":
            valid = _valid_url(value)
        elif field == "token":
            valid = isinstance(value, str) and len(value) <= 512 and all(33 <= ord(char) <= 126 for char in value)
        elif field == "tool_format":
            valid = value in ("native", "json")
        elif field in ("user_name", "device_name"):
            validate_voice_name(value, empty_ok=field == "user_name")
            valid = True
        elif field == "prompt":
            valid = _valid_text(value, 1024, extra_whitespace="\n\r\t")
        else:
            valid = _valid_text(value, 128 if field == "model" else 64)
        if not valid:
            raise ValueError


def encode_agent_config(patch):
    """Encode a UTF-8 patch. Errors never echo the supplied values."""
    try:
        if not isinstance(patch, dict) or not patch or not patch.keys() <= _ROOT_FIELDS:
            raise ValueError
        for key, value in patch.items():
            if key == "mode":
                if value not in AGENT_MODES:
                    raise ValueError
            elif key == "wake_enabled":
                if type(value) is not bool:
                    raise ValueError
            else:
                _validate_group(key, value)
        encoded = json.dumps(patch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_AGENT_CONFIG_BYTES:
            raise ValueError
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("板载助手配置包含无效字段、地址或长度。") from None
