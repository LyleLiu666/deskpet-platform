"""Create a Windows-only private directory for local SQLite files."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def state_path(path):
    """Place app-owned state under a dedicated directory when on Windows."""
    path = Path(path)
    if os.name != "nt" or path.parent.name == ".deskpet-private":
        return path
    return path.parent / ".deskpet-private" / path.name


def _libraries():
    return ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)


def current_user_sid():
    if os.name != "nt":
        raise OSError("Windows user identity is unavailable")
    advapi, kernel = _libraries()
    handle = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(handle, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(handle, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        value = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            kernel.LocalFree(value)
    finally:
        kernel.CloseHandle(handle)


def ensure_private_directory(path):
    """Restrict only our dedicated directory, never the caller's parent."""
    if os.name != "nt":
        raise OSError("Windows private directory is unavailable")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("private_directory_is_symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("invalid_private_directory")
    advapi, kernel = _libraries()
    descriptor = ctypes.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # The directory and new files inherit access only for this user and SYSTEM.
    sddl = f"D:P(A;OICI;FA;;;{current_user_sid()})(A;OICI;FA;;;SY)"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        acl = ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present.value:
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi.SetNamedSecurityInfoW(str(path), 1, 0x00000004 | 0x80000000, None, None, acl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)
