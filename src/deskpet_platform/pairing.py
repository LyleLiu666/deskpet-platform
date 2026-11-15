"""Private local credential storage. Pairing codes never enter MCP."""
import base64
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import uuid
from tempfile import NamedTemporaryFile

if os.name == "nt":
    DEFAULT_CONFIG = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "DeskPetPlatform/device.json"
else:
    DEFAULT_CONFIG = Path.home() / "Library/Application Support/DeskPetPlatform/device.json"
DEVICE_ID = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def valid_device_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return str(parsed) == value and parsed.version == 4


@dataclass(frozen=True)
class DeviceConfig:
    identity: str
    key: bytes = field(repr=False)
    address: str | None = None
    device_id: str | None = None

    def __post_init__(self):
        if not DEVICE_ID.fullmatch(self.identity) or len(self.key) != 32:
            raise ValueError("invalid_device_configuration")
        if self.device_id is not None and not valid_device_uuid(self.device_id):
            raise ValueError("invalid_device_uuid")
        if self.address:
            address = ipaddress.IPv4Address(self.address)
            ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            if not any(address in ipaddress.IPv4Network(network) for network in ranges):
                raise ValueError("address_must_be_private_ipv4")

    @classmethod
    def from_code(cls, code, address=None):
        match = re.fullmatch(r"D1-([0-9a-f]{12})-([A-Za-z0-9_-]{43})", code.strip())
        if not match:
            raise ValueError("配对码格式不正确，请在小板的「配对」页面重新扫描。")
        key = base64.urlsafe_b64decode(match[2] + "=")
        if base64.urlsafe_b64encode(key).decode().rstrip("=") != match[2]:
            raise ValueError("配对码格式不正确，请在小板的「配对」页面重新扫描。")
        identity = ":".join(match[1][index:index + 2] for index in range(0, 12, 2))
        return cls(identity, key, address)

    @classmethod
    def load(cls, path: Path):
        try:
            with path.open(encoding="utf-8") as stream:
                if os.name != "nt" and os.fstat(stream.fileno()).st_mode & 0o077:
                    raise ValueError("configuration_requires_mode_0600")
                value = json.load(stream)
            if os.name == "nt":
                from .windows_secrets import unprotect
                if not isinstance(value, dict) or set(value) != {"protected"}:
                    raise ValueError("configuration_requires_windows_protection")
                protected = base64.b64decode(value["protected"], validate=True)
                try:
                    value = json.loads(unprotect(protected))
                except OSError as error:
                    raise ValueError("configuration_cannot_be_unprotected") from error
            return cls(value["identity"], bytes.fromhex(value["key"]), value.get("address"), value.get("device_id"))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("设备配置无效，请检查权限或在本机重新配对。") from error

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = {"identity": self.identity, "key": self.key.hex(), "address": self.address}
        if self.device_id is not None:
            value["device_id"] = self.device_id
        if os.name == "nt":
            from .windows_secrets import protect
            data = json.dumps(value, separators=(",", ":")).encode("utf-8")
            value = {"protected": base64.b64encode(protect(data)).decode("ascii")}
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows cannot replace a still-open temporary file.
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
