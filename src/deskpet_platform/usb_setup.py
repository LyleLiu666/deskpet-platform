"""Provision one explicitly identified board over its local USB connection."""
import json
import os
from pathlib import Path
import stat
import time

from .pairing import DEVICE_ID, DeviceConfig
from .usb_link import default_serial_factory, valid_usb_port


def load_network(path):
    if os.name == "nt":
        raise ValueError("Windows 请使用小板上的二维码配网。")
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("Wi-Fi 凭据文件需要绝对路径。")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
                raise ValueError()
            values = json.loads(stream.read(1025))
        ssid, password = (values[name].encode("utf-8") for name in ("ssid", "password"))
        if not 1 <= len(ssid) <= 32 or (password and not 8 <= len(password) <= 63) or b"\0" in ssid + password:
            raise ValueError()
    except (OSError, AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("Wi-Fi 凭据文件无效，请检查文件权限和格式。") from None
    return ssid, password


def probe_usb(port, expected_identity, credentials_file=None, *, serial_factory=default_serial_factory):
    if not DEVICE_ID.fullmatch(expected_identity):
        raise ValueError("请提供登记的小板完整 MAC 地址。")
    if not valid_usb_port(port):
        raise ValueError("请提供已确认属于该板的 USB 串口绝对路径。")
    network = load_network(credentials_file) if credentials_file is not None else None
    with serial_factory(port=None, baudrate=115200, timeout=0.25, write_timeout=1, exclusive=True) as device:
        device.port, device.dtr, device.rts = port, False, False
        device.open()

        def request(command, parse, timeout=2):
            device.write(command + b"\n")
            deadline = time.monotonic() + timeout
            pending = bytearray()
            while time.monotonic() < deadline:
                pending.extend(device.read_until(size=128))
                if not pending.endswith(b"\n"):
                    if len(pending) >= 128:
                        pending.clear()
                    continue
                line = bytes(pending).strip()
                pending.clear()
                if line == b"DESK BUSY":
                    raise ValueError("小板正在被控制或配网，请结束当前操作后重试。")
                if line == b"DESK MISMATCH":
                    raise ValueError("USB 小板身份与登记值不符。")
                result = parse(line)
                if result is not None:
                    return result
            raise TimeoutError("USB 小板无响应，请检查串口和固件版本。")

        def parse_status(line):
            fields = line.decode("ascii", errors="replace").split()
            if len(fields) != 4 or fields[0] != "DESK" or not DEVICE_ID.fullmatch(fields[1]):
                return None
            if fields[1] != expected_identity:
                raise ValueError("USB 小板身份与登记值不符。")
            return fields[2], fields[3]

        def status():
            return request(b"DESK?", parse_status)

        def parse_configuration(line):
            if line in (b"DESK ACCEPTED", b"DESK INVALID"):
                return line == b"DESK ACCEPTED"
            return None

        def parse_pairing(line):
            if line == b"DESK UNAVAILABLE":
                raise ValueError("小板暂时无法生成配对码，请重试。")
            return line[len(b"DESK CODE "):] if line.startswith(b"DESK CODE ") else None

        try:
            address, phase = status()
        except TimeoutError:
            # Some USB adapters briefly reset the board when the port opens.
            address, phase = status()
        expected = expected_identity.encode("ascii")
        if network is not None:
            ssid, password = network
            response = request(b"DESK WIFI " + expected + b" " + ssid.hex().encode() + b" " +
                               (password.hex().encode() if password else b"-"), parse_configuration)
            if not response:
                raise ValueError("USB 配网请求未被设备接受。")
            phase = "connecting"
        deadline = time.monotonic() + 29
        while phase == "connecting" and time.monotonic() < deadline:
            time.sleep(0.3)
            address, phase = status()
        if phase != "online" and network is not None:
            if phase in ("restoring", "restored"):
                raise ValueError("小板未连上指定的 Wi-Fi，正在恢复或已恢复原有网络。")
            raise ValueError("小板未连上 Wi-Fi。请提供凭据文件，或使用屏幕上的配网二维码。")
        code = request(b"DESK PAIR " + expected, parse_pairing)
        config = DeviceConfig.from_code(code.decode("ascii"), address if phase in ("online", "restored") else None)
        if config.identity != expected_identity:
            raise ValueError("USB 配对凭据与登记的小板身份不符。")
        return config
