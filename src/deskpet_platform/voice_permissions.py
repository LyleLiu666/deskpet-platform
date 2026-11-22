"""Explicit per-pairing permissions for voice-controlled device actions."""
import hashlib
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import stat
import tempfile

from .file_lock import acquire, release
from .pairing import DeviceConfig

VOICE_TOOLS = {
    'device_status': '查看设备连接、页面和角色状态',
    'list_role_actions': '查看当前角色支持的动作',
    'play_role_action': '在工作台显示并播放当前角色的指定动作',
    'open_page': '临时打开设备支持的页面',
    'battery_status': '查看电池电压、估算电量和供电状态',
    'weather_status': '查看设备上的当前天气与数据状态',
    'quota_status': '查看设备收到的各来源额度与数据状态',
    'device_settings': '查看或临时调整音量、亮度',
    'device_restart': '请求在设备屏幕上确认软重启',
    'music_control': '查询、播放、暂停、停止、切歌或打乱设备曲库的播放顺序',
    'photo_slideshow': '开始或暂停相册轮播，或者临时调整轮播间隔',
}


def _unique_object(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate_key')
        value[key]=item
    return value


class VoicePermissions:
    def __init__(self, config_path):
        self.config_path=Path(config_path)
        self.path=self.config_path.with_name(self.config_path.name+'.voice-tools.json')

    def _identity(self):
        if self.config_path.is_symlink():raise ValueError('invalid_device_configuration')
        config=DeviceConfig.load(self.config_path)
        return hashlib.sha256(b'deskpet-voice-tools-v1\0'+config.identity.encode('ascii')+config.key).hexdigest()

    @asynccontextmanager
    async def lock(self):
        """Serialize permission updates with in-flight operations across local processes."""
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        name=self.path.with_name(self.path.name+'.lock')
        if name.is_symlink():raise ValueError('语音工具授权锁不可用。')
        fd=os.open(name,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        locked=False
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (os.name!='nt' and info.st_mode & 0o077):
                raise ValueError('语音工具授权锁不可用。')
            async with asyncio.timeout(5):
                while True:
                    try:acquire(fd,blocking=False);locked=True;break
                    except BlockingIOError:await asyncio.sleep(.05)
            yield
        finally:
            try:
                if locked:release(fd)
            finally:os.close(fd)

    def inspect(self):
        """Read on every use so other local service processes see revocations."""
        try:
            identity=self._identity()
        except FileNotFoundError:return frozenset(),'not_paired'
        except (OSError,ValueError,TypeError,UnicodeError,KeyError):return frozenset(),'invalid'
        try:
            if self.path.is_symlink():return frozenset(),'invalid'
            flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
            with os.fdopen(os.open(self.path,flags),'rb') as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or (os.name!='nt' and info.st_mode & 0o077):return frozenset(),'invalid'
                content=stream.read(4097)
            if len(content)>4096:return frozenset(),'invalid'
            data=json.loads(content,object_pairs_hook=_unique_object)
            if not isinstance(data,dict) or set(data)!={'version','pairing','enabled'} or type(data['version']) is not int or data['version']!=1:
                return frozenset(),'invalid'
            if data['pairing']!=identity:return frozenset(),'different_pairing'
            enabled=data['enabled']
            if (not isinstance(enabled,list) or any(not isinstance(name,str) or name not in VOICE_TOOLS for name in enabled) or
                    len(enabled)!=len(set(enabled))):return frozenset(),'invalid'
            return frozenset(enabled),'ok'
        except FileNotFoundError:return frozenset(),'ok'
        except (OSError,ValueError,TypeError,UnicodeError,KeyError):
            return frozenset(),'invalid'

    def enabled(self):
        return self.inspect()[0]

    def update(self, enabled):
        """Replace the entire explicit permission set; empty revokes all."""
        if (not isinstance(enabled,list) or any(not isinstance(name,str) or name not in VOICE_TOOLS for name in enabled) or
                len(enabled)!=len(set(enabled))):raise ValueError('语音工具列表无效。')
        try:identity=self._identity()
        except (OSError,ValueError,TypeError,UnicodeError):raise ValueError('请先完成设备配对。') from None
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.voice-tools-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as output:
                json.dump({'version':1,'pairing':identity,'enabled':sorted(enabled)},output,separators=(',',':'))
                output.flush();os.fsync(output.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
        return self.enabled()
