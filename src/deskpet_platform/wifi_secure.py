"""Small authenticated record format for a paired, memory-limited board."""
import hashlib
import hmac
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD=b'desk-link-v1'
MAX_PLAINTEXT=1152


def _base(identity,host_nonce,device_nonce):
    for value in (host_nonce,device_nonce):
        if len(value)!=32 or any(c not in '0123456789abcdef' for c in value):raise ValueError('invalid_nonce')
    if len(identity)!=17 or any(c not in '0123456789abcdef:' for c in identity):raise ValueError('invalid_identity')
    return f'desk-link-v1|{identity}|{host_nonce}|{device_nonce}'.encode()


def proof(key,role,identity,host_nonce,device_nonce):
    if role not in ('device','host'):raise ValueError('invalid_role')
    return hmac.new(key,role.encode()+b'|'+_base(identity,host_nonce,device_nonce),hashlib.sha256).hexdigest()


def session_key(key,identity,host_nonce,device_nonce):
    return hmac.new(key,b'session|'+_base(identity,host_nonce,device_nonce),hashlib.sha256).digest()


class RecordCodec:
    def __init__(self,key,send_direction,receive_direction):
        if len(key)!=32 or len(send_direction)!=4 or len(receive_direction)!=4 or send_direction==receive_direction:
            raise ValueError('invalid_session')
        self.aes=AESGCM(key);self.send_direction=send_direction;self.receive_direction=receive_direction
        self.sent=0;self.received=0

    def encode(self,plain):
        if not plain or len(plain)>MAX_PLAINTEXT:raise ValueError('invalid_record_size')
        self.sent+=1
        if self.sent>0xffffffffffffffff:raise ValueError('sequence_exhausted')
        nonce=self.send_direction+self.sent.to_bytes(8,'big')
        encrypted=self.aes.encrypt(nonce,plain,AAD)
        return b'F,'+str(self.sent).encode()+b','+encrypted.hex().encode()+b'\n'

    def decode(self,frame):
        try:
            if len(frame)>2400 or not frame.endswith(b'\n'):raise ValueError('invalid_record')
            prefix,number,hexdata=frame[:-1].split(b',',2)
            if prefix!=b'F' or not number.isdigit() or len(number)>20 or number.startswith(b'0'):raise ValueError('invalid_record')
            sequence=int(number)
            if sequence!=self.received+1 or sequence>0xffffffffffffffff:raise ValueError('invalid_sequence')
            encrypted=bytes.fromhex(hexdata.decode('ascii'))
            if len(encrypted)<=16 or len(encrypted)>MAX_PLAINTEXT+16:raise ValueError('invalid_record_size')
            plain=self.aes.decrypt(self.receive_direction+sequence.to_bytes(8,'big'),encrypted,AAD)
        except (UnicodeError,InvalidTag) as error:raise ValueError('invalid_record_authentication') from error
        self.received=sequence
        return plain
