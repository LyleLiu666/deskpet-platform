"""Read a single API key from a file private to the receiver account."""

import os
import stat


def read_private_key_file(path, *, max_bytes):
    if os.name != 'posix' or not os.path.isabs(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077:
                data = os.read(fd, max_bytes + 1)
                if len(data) <= max_bytes:
                    key = data.decode('ascii').removesuffix('\n').removesuffix('\r')
                    if key and all(33 <= ord(character) <= 126 for character in key):
                        return key
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError):
        return None
    return None
