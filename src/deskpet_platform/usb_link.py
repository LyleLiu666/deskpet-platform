"""Local serial transport for the authenticated-by-physical-access board session."""
import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time

import serial

try:
    import fcntl
    import termios
except ImportError:  # Windows uses pyserial without POSIX ioctl.
    fcntl = termios = None

from .device_lock import DeviceLock
from .pairing import DEVICE_ID, valid_device_uuid


class MacSerial(serial.Serial):
    """Release both reset lines in one ioctl during pyserial's open sequence."""

    def _update_dtr_state(self):
        if self.is_open:
            return super()._update_dtr_state()
        # The CH340 can reset the ESP32 between pyserial's DTR and RTS updates.
        lines = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
        fcntl.ioctl(self.fd, termios.TIOCMBIC, lines)

    def _update_rts_state(self):
        if self.is_open:
            return super()._update_rts_state()


default_serial_factory = MacSerial if sys.platform == "darwin" else serial.Serial


def valid_usb_port(path, *, windows=None):
    if not isinstance(path, str):
        return False
    if windows is None:
        windows = sys.platform == "win32"
    if windows:
        match = re.fullmatch(r"COM([1-9][0-9]{0,3})", path, flags=re.IGNORECASE)
        return bool(match and int(match[1]) <= 4096)
    return path.startswith("/dev/")


def windows_port_identity(path, ports):
    matches = [port for port in ports if port.device.upper() == path.upper()]
    if not matches:
        raise FileNotFoundError(path)
    if len(matches) != 1:
        raise ValueError("USB 串口标识不唯一，请重新确认设备。")
    port = matches[0]
    return tuple(getattr(port, field, None) for field in ("vid", "pid", "serial_number", "location", "hwid"))


@dataclass(frozen=True)
class UsbPort:
    path: str
    node: tuple | None

    @staticmethod
    def _identity(path):
        if sys.platform == "win32":
            from serial.tools import list_ports
            return windows_port_identity(path, list_ports.comports())
        info = os.stat(path)
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError("请选择 USB 串口设备。")
        return info.st_ino, info.st_rdev, getattr(info, "st_birthtime", info.st_ctime_ns)

    @classmethod
    def register(cls, path, *, allow_missing=False):
        if not valid_usb_port(path):
            raise ValueError("请提供已确认属于该板的 USB 串口绝对路径。")
        try:
            node = cls._identity(path)
        except FileNotFoundError:
            if not allow_missing:
                raise
            node = None
        return cls(path, node)

    def verify(self):
        if self.node is None or self._identity(self.path) != self.node:
            raise OSError("usb_port_changed: 请重新确认调试板串口。")


@dataclass(frozen=True)
class UsbIdentity:
    identity: str
    device_id: str | None


def read_usb_identity(port, *, serial_factory=default_serial_factory):
    """Read a board's identity without requesting control or resetting it."""
    registration = port if isinstance(port, UsbPort) else UsbPort.register(port)
    registration.verify()
    device = serial_factory(port=None, baudrate=115200, timeout=0.25, write_timeout=1, exclusive=True)
    try:
        device.port, device.dtr, device.rts = registration.path, False, False
        device.open()
        registration.verify()
        device.write(b"DESK IDENTITY?\n")
        deadline = time.monotonic() + 2
        pending = bytearray()
        while time.monotonic() < deadline:
            pending.extend(device.read_until(size=128))
            if not pending.endswith(b"\n"):
                if len(pending) >= 128:
                    pending.clear()
                continue
            fields = pending.decode("ascii", errors="replace").split()
            pending.clear()
            if len(fields) != 4 or fields[:2] != ["DESK", "IDENTITY"]:
                continue
            _, _, identity, value = fields
            if not DEVICE_ID.fullmatch(identity) or (value != "-" and not valid_device_uuid(value)):
                raise ValueError("小板返回的身份格式无效。")
            registration.verify()
            return UsbIdentity(identity, None if value == "-" else value)
        raise TimeoutError("USB 小板未返回 UUID，请检查串口和固件版本。")
    finally:
        device.close()


def find_usb_devices(ports, device_id=None):
    """Probe only the explicitly selected ports without acquiring control."""
    if not 1 <= len(ports) <= 8 or len(set(ports)) != len(ports):
        raise ValueError("请指定 1 至 8 个不同的小板 USB 串口。")
    if device_id is not None and not valid_device_uuid(device_id):
        raise ValueError("请提供有效的小板 UUID。")
    devices, unavailable = [], []
    for port in ports:
        try:
            observed = read_usb_identity(port)
        except (OSError, ValueError) as error:
            unavailable.append({"port": port, "reason": str(error)})
            continue
        if device_id is None or observed.device_id == device_id:
            devices.append({"port": port, **asdict(observed)})
    return {"devices": devices, "unavailable": unavailable}


class UsbLink:
    wireless = False
    peer_address = None

    def __init__(self, identity, _key, *, port, peer_hint=None, serial_factory=default_serial_factory, lock_root=None):
        path = port.path if isinstance(port, UsbPort) else port
        if not valid_usb_port(path):
            raise ValueError("请提供已确认属于该板的 USB 串口绝对路径。")
        self.identity, self.port, self.registration = identity, path, port if isinstance(port, UsbPort) else None
        self.serial_factory, self.lock_root = serial_factory, lock_root
        self.device = self.lease = None

    def _open(self):
        if self.registration is not None:
            self.registration.verify()
        device = self.serial_factory(port=None, baudrate=115200, timeout=0.25, write_timeout=2, exclusive=True)
        try:
            device.port, device.dtr, device.rts = self.port, False, False
            device.open()
            if self.registration is not None:
                self.registration.verify()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                device.write(f"DESK CONTROL {self.identity}\n".encode("ascii"))
                line = device.read_until(size=128).strip()
                if line == b"DESK CONTROL READY":
                    return device
                if line == b"DESK MISMATCH":
                    raise ValueError("USB 小板身份与登记值不符。")
                if line == b"DESK BUSY":
                    raise RuntimeError("USB 小板已有 Wi-Fi 控制连接。")
            raise TimeoutError("USB 小板无响应，请检查串口和固件版本。")
        except BaseException:
            device.close()
            raise

    async def open(self):
        self.lease = DeviceLock(self.identity, self.lock_root)
        self.lease.__enter__()
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            self.device = await asyncio.shield(opening)
        except asyncio.CancelledError:
            with suppress(OSError, RuntimeError, ValueError):
                self.device = await opening
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def write(self, data):
        if self.device is None:
            raise OSError("usb_disconnected")
        await asyncio.to_thread(self.device.write, data)

    async def readline(self):
        pending = bytearray()
        while self.device is not None:
            pending.extend(await asyncio.to_thread(self.device.read_until, size=2048))
            if len(pending) > 2048:
                raise OSError("usb_receive_overflow")
            if pending.endswith(b"\n"):
                if pending.startswith(b"P1,"):
                    return bytes(pending).rstrip(b"\r\n") + b"\n"
                pending.clear()
        raise OSError("usb_disconnected")

    async def close(self):
        try:
            if self.device is not None:
                device, self.device = self.device, None
                with suppress(OSError):
                    await asyncio.to_thread(device.write, b"DESK END\n")
                    await asyncio.to_thread(device.flush)
                await asyncio.to_thread(device.close)
        finally:
            if self.lease is not None:
                self.lease.__exit__(None, None, None)
                self.lease = None
