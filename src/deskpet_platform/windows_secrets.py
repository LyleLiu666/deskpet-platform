"""Protect pairing data with Windows' per-user Data Protection API."""

import ctypes
from ctypes import wintypes
import os


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _transform(data, *, protect):
    if os.name != "nt":
        raise OSError("Windows credential protection is unavailable")
    if not isinstance(data, bytes) or not data:
        raise ValueError("empty Windows credential")
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source_blob = _DataBlob(len(data), source)
    output_blob = _DataBlob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = library.CryptProtectData if protect else library.CryptUnprotectData
    function.restype = wintypes.BOOL
    function.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # 0x1 forbids interactive prompts; without LOCAL_MACHINE protection is per user.
    if not function(ctypes.byref(source_blob), None, None, None, None, 0x1, ctypes.byref(output_blob)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel.LocalFree(output_blob.data)


def protect(data):
    return _transform(data, protect=True)


def unprotect(data):
    return _transform(data, protect=False)
