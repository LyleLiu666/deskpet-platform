"""Validation shared by the device name interface and its reply checks."""

def validate_voice_name(value, *, empty_ok):
    try:
        valid = (isinstance(value, str) and len(value.encode('utf-8')) <= 64 and
                 (bool(value) or empty_ok) and not value.startswith(' ') and not value.endswith(' ') and
                 all(not (ord(c)<32 or 0x7f<=ord(c)<=0x9f or ord(c) in (0x2028,0x2029)) for c in value))
    except UnicodeError:
        valid = False
    if not valid:raise ValueError('昵称格式无效：请使用不超过 64 字节的单行文字，设备昵称不能为空。')
    return value
