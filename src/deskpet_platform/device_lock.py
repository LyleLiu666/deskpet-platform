"""One owner per board on this Mac, shared with the original DeskPet host."""
import hashlib
import os
from pathlib import Path

from .file_lock import acquire


class DeviceLock:
    def __init__(self,identity,root=None):
        if root:
            self.root=Path(root)
        elif os.name == 'nt':
            self.root=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')/'DeskPetPlatform/device-locks'
        else:
            self.root=Path.home()/'Library/Caches/DeskPet/device-locks'
        self.name=hashlib.sha256(identity.encode()).hexdigest()+'.lock'
        self.file=None
    def __enter__(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.root.chmod(0o700)
        self.file=open(self.root/self.name,'a+')
        os.chmod(self.root/self.name,0o600)
        try:acquire(self.file.fileno(), blocking=False)
        except BlockingIOError:
            self.file.close();self.file=None
            raise RuntimeError('device_busy: another owner holds device lock') from None
        return self
    def __exit__(self,*exc):
        if self.file:self.file.close();self.file=None
