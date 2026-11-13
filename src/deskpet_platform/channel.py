"""One-in-flight device channel. A timeout invalidates the epoch, never retries a play."""
import asyncio
import json
import time
import uuid
from collections import deque

# Wi-Fi authenticates plaintext records up to this size; USB's receive limit is
# larger. Requests retain their separate 1024-byte bound.
MAX_RESPONSE_BYTES = 1152


class DeviceError(RuntimeError):
    pass


class AckTimeout(DeviceError):
    pass


class CommandRejected(DeviceError):
    """The device received the command and returned an application error."""

    def __init__(self, code, stage=None):
        self.code = code
        self.stage = stage
        super().__init__('device_error: ' + code + (f' ({stage})' if stage else ''))


class Channel:
    def __init__(self, transport, expected_mac, timeout=None):
        self.transport=transport
        self.expected_mac=expected_mac
        # The board's TCP stack can take 3 seconds for its first retransmit.
        # Keep the same request in flight so an operation is never sent twice.
        self.timeout=timeout if timeout is not None else (5 if getattr(transport,'wireless',False) else 2)
        self.lock=asyncio.Lock()
        self.epoch=''
        self.seq=0
        self.boot=None
        self.online=False
        self.capabilities={}
        self.events=asyncio.Queue(maxsize=16)
        self.dropped_events=0

    async def handshake(self):
        async with self.lock:
            self.online=False
            while not self.events.empty():self.events.get_nowait()
            self.epoch=uuid.uuid4().hex
            self.seq=0
            self.boot=None
            response=await self._exchange('hello',{})
            if response.get('mac')!=self.expected_mac or response.get('protocol')!=1 or response.get('rx_bytes',0)<2048:
                raise DeviceError('identity_mismatch: unsupported device or receive capacity')
            self.boot=response['boot']
            self.capabilities=response
            self.online=True
            return response

    def capture_event(self, raw):
        if not raw.startswith(b'P1,'):return False
        if len(raw)>MAX_RESPONSE_BYTES or not raw.endswith(b'\n'):raise DeviceError('invalid_response')
        try:message=json.loads(raw[3:])
        except (ValueError,UnicodeError):raise DeviceError('invalid_response') from None
        if not isinstance(message,dict):raise DeviceError('invalid_response')
        if message.get('type')!='touch':return False
        if message.get('epoch')!=self.epoch:return True
        if self.boot is None:return True
        if message.get('boot')!=self.boot:raise DeviceError('device_restarted')
        base={'type','epoch','boot','operation','version','id','action'}
        extra=set(message)-base
        if not base.issubset(message) or extra not in (set(),{'x','y'},{'x','y','notification_id'}):
            raise DeviceError('invalid_touch')
        if 'notification_id' in message and (message['action']!='notice_seen' or
                                              type(message['notification_id']) is not int or
                                              not 0<message['notification_id']<=0xffffffff):
            raise DeviceError('invalid_touch')
        if 'x' in message and (type(message['x']) is not int or not 0<=message['x']<320 or
                               type(message['y']) is not int or not 0<=message['y']<240):
            raise DeviceError('invalid_touch')
        if any(type(message[k]) is not int or not 0<=message[k]<=0xffffffff for k in ('operation','version')) or not message['operation']:
            raise DeviceError('invalid_touch')
        if not isinstance(message['id'],str) or len(message['id'])>35 or not message['id'].isascii():
            raise DeviceError('invalid_touch')
        if not isinstance(message['action'], str) or not 0 < len(message['action']) <= 64 or not message['action'].isascii():
            raise DeviceError('invalid_touch')
        if self.events.full():
            self.events.get_nowait()
            self.dropped_events+=1
        self.events.put_nowait(message)
        return True

    async def send(self, message_type, *, clock_epoch=None, timeout=None, **fields):
        async with self.lock:
            if not self.online:
                raise DeviceError('handshake_required')
            return await self._exchange(message_type,fields,clock_epoch,timeout)

    async def _exchange(self, kind, fields, clock_epoch=None, timeout=None):
        if {'epoch','seq','type'} & fields.keys():
            raise DeviceError('invalid_fields: reserved envelope')
        self.seq+=1
        line=('P1,'+json.dumps(dict(epoch=self.epoch,seq=self.seq,type=kind,**fields),separators=(',',':'),ensure_ascii=True)+'\n').encode()
        if len(line)>1024:
            raise DeviceError('line_too_long')
        batch=line
        if clock_epoch is not None:
            if type(clock_epoch) is not int or not 1700000000<clock_epoch<2147483647:
                raise DeviceError('invalid_clock')
            batch=f'T1,{clock_epoch}\n'.encode()+line
        if len(batch)>1024:
            raise DeviceError('batch_too_long')
        ignored=deque(maxlen=8)
        try:
            timeout=self.timeout if timeout is None else timeout
            deadline=time.monotonic()+timeout
            await asyncio.wait_for(self.transport.write(batch),timeout)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise asyncio.TimeoutError
                raw=await asyncio.wait_for(self.transport.readline(),remaining)
                if self.capture_event(raw):ignored.append('touch');continue
                if not raw.startswith(b'P1,'):
                    ignored.append('other')
                    continue
                if len(raw)>MAX_RESPONSE_BYTES or not raw.endswith(b'\n'):
                    raise DeviceError('invalid_response')
                try:
                    response=json.loads(raw[3:])
                except (ValueError,UnicodeError):
                    raise DeviceError('invalid_response') from None
                if not isinstance(response,dict) or type(response.get('boot')) is not int:
                    raise DeviceError('invalid_response')
                if response.get('epoch')!=self.epoch or response.get('seq')!=self.seq:
                    ignored.append('stale')
                    continue
                if self.boot is not None and response.get('boot')!=self.boot:
                    raise DeviceError('device_restarted')
                if response.get('ok') is False:
                    raise CommandRejected(str(response.get('error','unknown')), response.get('stage'))
                if response.get('ok') is not True:
                    raise DeviceError('invalid_response')
                return response
        except CommandRejected:
            raise
        except asyncio.TimeoutError:
            self.online=False
            raise AckTimeout(f'ack_timeout: {kind}; seq={self.seq}; ignored={list(ignored)}') from None
        except (DeviceError,OSError,asyncio.CancelledError):
            self.online=False
            raise
