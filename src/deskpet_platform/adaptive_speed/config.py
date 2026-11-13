"""User-selected comparison windows; defaults retain the original product design."""
from dataclasses import dataclass

RECENT_CHOICES=(120,300,600,1800,3600)
LONG_CHOICES=(1,3,7)

@dataclass(frozen=True)
class Config:
    recent_seconds: int = 1800
    long_days: int = 7

    def __post_init__(self):
        if type(self.recent_seconds) is not int or self.recent_seconds not in RECENT_CHOICES or type(self.long_days) is not int or self.long_days not in LONG_CHOICES:
            raise ValueError('invalid_speed_windows')

    @property
    def recent_minimum(self):return self.recent_seconds//6

    @classmethod
    def from_setting(cls,value):
        try:return cls(**value) if value else cls()
        except (ValueError,TypeError):return cls()

    @classmethod
    def from_touch(cls,value):
        recent,days=value.split(':')
        config=cls(int(recent),int(days))
        if f'{config.recent_seconds}:{config.long_days}'!=value:raise ValueError('invalid_speed_windows')
        return config
