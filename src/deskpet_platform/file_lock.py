"""Exclusive advisory locks used by local DeskPet processes."""

import errno
import os
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire(fd, *, blocking=True):
    """Lock the same one-byte region on Windows, or the whole file on POSIX."""
    if os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if not blocking:
                raise BlockingIOError(error.errno, "file lock is held") from error
            time.sleep(0.05)


def release(fd):
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
