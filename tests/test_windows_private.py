import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


def dacl_trustees(path):
    """Read the actual DACL independently from the writer using Windows APIs."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    error = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        sddl = wintypes.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(sddl), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return re.findall(r"\(A;[^;]*;[^;]*;[^;]*;[^;]*;([^;)]+)\)", sddl.value)
        finally:
            kernel.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))
    finally:
        kernel.LocalFree(descriptor)


@unittest.skipUnless(os.name == "nt", "Windows private storage")
class WindowsPrivateStorageTests(unittest.TestCase):
    def test_dedicated_directory_and_new_files_are_restricted_and_writable(self):
        from deskpet_platform.windows_private import current_user_sid, ensure_private_directory
        with tempfile.TemporaryDirectory(prefix="deskpet-private-") as directory:
            root = Path(directory) / ".deskpet-private"
            ensure_private_directory(root)
            path = root / "fixture.txt"
            path.write_text("synthetic content", encoding="utf-8")
            ensure_private_directory(root)
            self.assertEqual(path.read_text(encoding="utf-8"), "synthetic content")
            for item in (root, path):
                sids = dacl_trustees(item)
                user_sid = current_user_sid()
                # SDDL abbreviates a built-in administrator's RID 500 as LA.
                expected_user = "LA" if user_sid.endswith("-500") and "LA" in sids else user_sid
                self.assertIn(expected_user, sids)
                self.assertTrue(set(sids) <= {expected_user, "SY", "S-1-5-18"}, sids)

    def test_dedicated_directory_must_not_be_a_junction(self):
        from deskpet_platform.windows_private import ensure_private_directory
        with tempfile.TemporaryDirectory(prefix="deskpet-private-") as directory:
            root = Path(directory)
            target = root / "someone-else"
            target.mkdir()
            before = dacl_trustees(target)
            link = root / ".deskpet-private"
            subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True, timeout=10)
            try:
                with self.assertRaises(ValueError):
                    ensure_private_directory(link)
                self.assertEqual(dacl_trustees(target), before)
            finally:
                link.rmdir()
