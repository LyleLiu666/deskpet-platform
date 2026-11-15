import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from deskpet_platform.file_lock import acquire, release


class FileLockTests(unittest.TestCase):
    def test_exclusive_nonblocking_lock_and_release(self):
        with tempfile.TemporaryDirectory(prefix="deskpet-lock-") as directory:
            path = Path(directory) / "board.lock"
            first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                acquire(first)
                with self.assertRaises(BlockingIOError):
                    acquire(second, blocking=False)
                release(first)
                acquire(second, blocking=False)
                release(second)
            finally:
                os.close(first)
                os.close(second)

    def test_lock_excludes_a_different_process(self):
        with tempfile.TemporaryDirectory(prefix="deskpet-lock-") as directory:
            path = Path(directory) / "board.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                acquire(fd)
                program = """import os,sys
from deskpet_platform.file_lock import acquire
fd=os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
try:
    acquire(fd, blocking=False)
except BlockingIOError:
    print('busy')
else:
    print('available')
os.close(fd)
"""
                result = subprocess.run([sys.executable, "-c", program, str(path)], capture_output=True, text=True, timeout=5, check=True)
                self.assertEqual(result.stdout.strip(), "busy")
                release(fd)
                result = subprocess.run([sys.executable, "-c", program, str(path)], capture_output=True, text=True, timeout=5, check=True)
                self.assertEqual(result.stdout.strip(), "available")
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
