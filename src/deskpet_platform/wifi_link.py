"""Authenticated local Wi-Fi transport for the existing device protocol."""
import asyncio
from contextlib import suppress
import hmac
from ipaddress import IPv4Address
import secrets
import socket
from .device_lock import DeviceLock
from .wifi_secure import RecordCodec,proof,session_key


DISCOVERY_PORT=34915
DIRECT_PREFIX = b"DESK-DIRECT-1,"
NO_DEVICE_REPLY_HELP = (
    "没有收到托肯的连接。确认电脑和托肯在同一局域网，自动发现失败时在配对页填写托肯显示的 IP。"
    "如使用 VPN 或代理，请检查局域网流量是否通行。如果托肯使用需要回连电脑的旧固件，"
    "请检查 Windows 防火墙是否允许 DeskPet 的 Python 进程在当前受信任的网络接收入站连接。"
    "macOS 请检查局域网权限。"
)


async def probe_board(identity, key, address, *, port=DISCOVERY_PORT, timeout=1):
    """Check reachability using the reference firmware's authenticated UDP probe."""
    prefix = b"DESK-PROBE-1,"
    nonce = secrets.token_hex(16)
    message = f"probe|desk-link-v1|{identity}|{nonce}|".encode()
    expected = prefix + hmac.digest(key, message, "sha256")[:16].hex().encode()
    loop = asyncio.get_running_loop()
    try:
        ip = IPv4Address(address)
        if ip.is_multicast or ip.is_unspecified or int(ip) == 0xFFFFFFFF:
            return False
        peer = (str(ip), port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setblocking(False)
            async with asyncio.timeout(timeout):
                await loop.sock_sendto(udp, prefix + f"{identity},{nonce}".encode(), peer)
                while True:
                    reply, source = await loop.sock_recvfrom(udp, 96)
                    if source == peer:
                        return hmac.compare_digest(reply, expected)
    except (OSError, ValueError):
        return False


class WifiLink:
    wireless=True
    def __init__(self,identity,key,*,lock_root=None,discovery_host='255.255.255.255',discovery_port=DISCOVERY_PORT,peer_hint=None):
        if not isinstance(key,bytes) or len(key)!=32:raise ValueError('wifi_key must be 32 bytes')
        self.identity=identity;self.key=key;self.lock_root=lock_root
        self.discovery_host=discovery_host;self.discovery_port=discovery_port
        self.peer_hint=peer_hint;self.peer_address=None
        self.connection_direction = None
        self.queue=asyncio.Queue(maxsize=32)
        self.closed=True
        self.lease=None;self.reader=None;self.writer=None;self.stream=None;self.codec=None
        self.write_lock=asyncio.Lock()

    async def _authenticate(self, reader, writer, *, already_connected=None):
        host_nonce = secrets.token_hex(16)
        writer.write(f"H1,{self.identity},{host_nonce}\n".encode())
        await writer.drain()
        line = await reader.readline()
        prefix, device_nonce, signature = line.decode("ascii").strip().split(",")
        expected = proof(self.key, "device", self.identity, host_nonce, device_nonce)
        if prefix != "D1" or not hmac.compare_digest(signature, expected):
            raise ValueError("wifi_device_authentication_failed")
        if already_connected is not None and already_connected():
            raise ValueError("wifi_link_already_open")
        host_proof = proof(self.key, "host", self.identity, host_nonce, device_nonce)
        writer.write(f"H2,{host_proof}\n".encode())
        await writer.drain()
        return RecordCodec(session_key(self.key, self.identity, host_nonce, device_nonce), b"HOST", b"DEVC")

    async def _discover_direct(self, udp, targets, timeout):
        loop = asyncio.get_running_loop()
        nonce = secrets.token_hex(16)
        request = DIRECT_PREFIX + f"{self.identity},{nonce}".encode()
        message = f"direct|desk-link-v1|{self.identity}|{nonce}|".encode()
        expected = DIRECT_PREFIX + hmac.digest(self.key, message, "sha256")[:16].hex().encode()
        for _ in range(2):
            for host in targets:
                with suppress(OSError):
                    udp.sendto(request, (host, self.discovery_port))
            try:
                async with asyncio.timeout(timeout / 2):
                    while True:
                        reply, source = await loop.sock_recvfrom(udp, 96)
                        if source[1] == self.discovery_port and hmac.compare_digest(reply, expected):
                            return source
            except TimeoutError:
                pass
        return None

    async def _open_direct(self, udp, targets, budget):
        peer = await self._discover_direct(udp, targets, min(0.8, budget / 4))
        if peer is None:
            return None
        writer = None
        try:
            async with asyncio.timeout(min(3, budget / 2)):
                reader, writer = await asyncio.open_connection(*peer, limit=4096)
                codec = await self._authenticate(reader, writer)
            return reader, writer, codec
        except (OSError, ValueError, TimeoutError):
            if writer is not None:
                writer.close()
            return None
        except BaseException:
            if writer is not None:
                writer.close()
            raise

    def _start_stream(self, connection, direction):
        self.stream, self.writer, self.codec = connection
        self.closed = False
        self.connection_direction = direction
        self.peer_address = self.writer.get_extra_info("peername")[0]
        self.reader = asyncio.create_task(self._read_loop())

    async def open(self,timeout=12):
        if self.lease is not None:
            raise RuntimeError('wifi_link_already_open')
        self.queue = asyncio.Queue(maxsize=32)
        lease = DeviceLock(self.identity, self.lock_root)
        lease.__enter__()
        self.lease = lease
        listener=None;udp=None;handshakes=set();accepted=None
        try:
            accepted=asyncio.get_running_loop().create_future()
            def accept(reader,writer):
                if accepted.done() or len(handshakes)>=4:writer.close();return
                task=asyncio.create_task(authenticate(reader,writer));handshakes.add(task);task.add_done_callback(handshakes.discard)
            async def authenticate(reader,writer):
                retained=False
                try:
                    async with asyncio.timeout(5):
                        codec = await self._authenticate(reader, writer, already_connected=accepted.done)
                    if accepted.done():
                        return
                    accepted.set_result((reader,writer,codec));retained=True
                except (OSError,ValueError,UnicodeError,asyncio.TimeoutError):pass
                finally:
                    if not retained:writer.close()
            udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            udp.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);udp.setblocking(False)
            deadline=asyncio.get_running_loop().time()+timeout
            targets=tuple(dict.fromkeys(host for host in (self.peer_hint,self.discovery_host) if host))
            connection = await self._open_direct(udp, targets, timeout)
            if connection is not None:
                self._start_stream(connection, "direct")
                return
            listener=await asyncio.start_server(accept,'0.0.0.0',0,limit=4096)
            port=listener.sockets[0].getsockname()[1]
            beacon=f'DESK-LINK-1,{self.identity},{port}'.encode()
            sent=False;last_error=None
            while not accepted.done():
                remaining=deadline-asyncio.get_running_loop().time()
                if remaining<=0:
                    if not sent:raise OSError('wifi_discovery_unreachable') from last_error
                    raise OSError('wifi_device_absent')
                for host in targets:
                    try:
                        udp.sendto(beacon,(host,self.discovery_port))
                        sent=True
                    except OSError as error:
                        last_error=error
                try:await asyncio.wait_for(asyncio.shield(accepted),min(1.5,remaining))
                except asyncio.TimeoutError:pass
            self._start_stream(accepted.result(), "callback")
        except BaseException:
            await self.close();raise
        finally:
            if udp:udp.close()
            # Stop accepting new sockets while retaining the authenticated stream.
            if listener:listener.close()
            for task in handshakes:task.cancel()
            if handshakes:await asyncio.gather(*handshakes,return_exceptions=True)
            if accepted and accepted.done() and not accepted.cancelled() and self.writer is None:
                accepted.result()[1].close()

    async def _read_loop(self):
        try:
            pending=bytearray()
            while not self.closed:
                frame=await self.stream.readline()
                if not frame:raise OSError('wifi_disconnected')
                pending.extend(self.codec.decode(frame))
                while b'\n' in pending:
                    end=pending.index(b'\n')+1;line=bytes(pending[:end]);del pending[:end]
                    if len(line)>2048:raise OSError('wifi_line_overflow')
                    if line.startswith(b'P1,'):
                        if self.queue.full():raise OSError('wifi_receive_overflow')
                        self.queue.put_nowait(line)
                if len(pending)>2048:raise OSError('wifi_line_overflow')
        except (OSError,ValueError,asyncio.IncompleteReadError) as error:
            if not self.closed:
                if self.queue.full():
                    while not self.queue.empty():self.queue.get_nowait()
                self.queue.put_nowait(OSError('wifi_disconnected: '+str(error)))

    async def write(self,data):
        if self.closed or not self.writer:raise OSError('wifi_disconnected')
        async with self.write_lock:
            self.writer.write(self.codec.encode(data));await self.writer.drain()

    async def readline(self):
        value=await self.queue.get()
        if isinstance(value,Exception):raise value
        return value

    async def close(self):
        self.closed=True
        self.connection_direction = None
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):await self.reader
            self.reader=None
        if self.writer:
            self.writer.close()
            with suppress(OSError,asyncio.TimeoutError):await asyncio.wait_for(self.writer.wait_closed(),2)
            self.writer=None;self.stream=None;self.codec=None
        if self.lease:self.lease.__exit__(None,None,None);self.lease=None
