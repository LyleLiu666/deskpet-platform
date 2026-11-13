"""Authenticated, bounded file transfer to the paired board's storage card."""
import asyncio
from contextlib import suppress
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
from pathlib import Path
import re
import secrets
import shutil
import socket
import tempfile
import threading
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .channel import CommandRejected

MAX_FILE_BYTES = 128 * 1024 * 1024


def validate_path(path):
    if not isinstance(path, str) or len(path) > 110 or not path.startswith(("roles/", "desk/", "photos/")):
        raise ValueError("储存卡路径无效。")
    if any(not part or part.startswith(".") or part.lower().endswith((".upload", ".previous", ".removing")) or
           not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
           for part in path.split("/")):
        raise ValueError("储存卡路径无效。")


def file_info(source):
    size = crc = 0
    with source.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ValueError("文件超过 128 MiB 限制。")
            crc = zlib.crc32(chunk, crc)
    if not size:
        raise ValueError("不能传输空文件。")
    return size, crc


class CardRelay:
    def __init__(self, device_ip, encrypted):
        self.device_ip = device_ip
        self.encrypted = encrypted
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.served = False
        self.closed = False
        self.connection = None

    def start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((self.device_ip, 9))
            host = probe.getsockname()[0]
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                with relay.lock:
                    permitted = (self.client_address[0] == relay.device_ip and
                                 self.path == "/" + relay.token and not relay.served and not relay.closed)
                    if permitted:
                        relay.served = True
                        relay.connection = self.connection
                if not permitted:
                    self.send_error(404)
                    return
                try:
                    self.connection.settimeout(15)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(relay.encrypted.stat().st_size))
                    self.end_headers()
                    with relay.encrypted.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile, length=128 * 1024)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://{host}:{self.server.server_port}/{self.token}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self):
        with self.lock:
            self.closed = True
            if self.connection is not None:
                with suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def encrypt_file(source, destination, key, iv, path, size, crc):
    derived = hmac.new(key, b"desk-card-v1/key", hashlib.sha256).digest()
    encryptor = Cipher(algorithms.AES(derived), modes.GCM(iv)).encryptor()
    encryptor.authenticate_additional_data(f"desk-card-v1\n{path}\n{size}\n{crc:08x}".encode("ascii"))
    digest, actual_size, actual_crc = hashlib.sha256(), 0, 0
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(256 * 1024):
            digest.update(chunk)
            actual_size += len(chunk)
            actual_crc = zlib.crc32(chunk, actual_crc)
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)
    if actual_size != size or actual_crc != crc:
        raise ValueError("文件在传输准备期间发生变化，请重试。")
    return digest.hexdigest()


async def upload_file(channel, device_ip, key, path, source, *, expected_sha256=None, background=False):
    validate_path(path)
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("设备配对凭据不可用。")
    source = Path(source)
    size, crc = await asyncio.to_thread(file_info, source)
    # Allow for SPI card writes and readback while the role is still playing.
    transfer_timeout = max(300, 60 + math.ceil(size / (48 * 1024)))
    iv = secrets.token_bytes(12)
    with tempfile.TemporaryDirectory(prefix="deskpet-card-") as directory:
        encrypted = Path(directory) / "payload"
        actual_sha256 = await asyncio.to_thread(encrypt_file, source, encrypted, key, iv, path, size, crc)
        if expected_sha256 is not None and not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError("角色文件自检查后已变化，请重新安装。")
        # Each HTTP retry uses a fresh one-use address, as in the reference controller.
        for attempt in range(3):
            relay = CardRelay(device_ip, encrypted).start()
            try:
                fields = dict(path=path, bytes=size, crc32=crc, iv=iv.hex(), url=relay.url)
                if background:
                    async with asyncio.timeout(transfer_timeout):
                        result = await channel.send("card_fetch", background=True, **fields)
                        operation = result["seq"]
                        while result.get("transfer_status") == "pending":
                            # The card works independently; frequent status queries
                            # compete with the transfer and the normal heartbeat.
                            await asyncio.sleep(1)
                            result = await channel.send("card_fetch_status", operation=operation)
                else:
                    result = await channel.send("card_fetch", timeout=transfer_timeout, **fields)
                if result.get("stored") is not True or (not relay.served and result.get("unchanged") is not True):
                    raise RuntimeError("设备未确认储存卡文件已写入。")
                break
            except CommandRejected as error:
                if attempt == 2 or error.stage not in ("http_open", "http_headers", "http_body", "http_tag"):
                    raise
            finally:
                await asyncio.to_thread(relay.close)
    return {"path": path, "bytes": size, "crc32": f"{crc:08x}",
            "unchanged": result.get("unchanged") is True}


async def upload_usb(channel, path, source, *, expected_sha256=None):
    validate_path(path)
    source = Path(source)
    size, crc = await asyncio.to_thread(file_info, source)
    result = await channel.send("card_usb_begin", path=path, bytes=size, crc32=crc, timeout=300)
    unchanged = result.get("unchanged") is True
    if not unchanged:
        offset, digest = 0, hashlib.sha256()
        try:
            with source.open("rb") as stream:
                while block := stream.read(384):
                    result = await channel.send("card_usb_chunk", offset=offset, data=block.hex())
                    offset += len(block)
                    digest.update(block)
                    if result.get("offset") != offset:
                        raise RuntimeError("小板储存卡写入位置不符。")
            if expected_sha256 is not None and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                raise ValueError("角色文件自检查后已变化，请重新安装。")
            result = await channel.send("card_usb_commit", timeout=300)
            if result.get("stored") is not True:
                raise RuntimeError("设备未确认储存卡文件已写入。")
        except BaseException:
            if channel.online:
                with suppress(OSError, RuntimeError):
                    await channel.send("card_usb_abort")
            raise
    return {"path": path, "bytes": size, "crc32": f"{crc:08x}", "unchanged": unchanged}
