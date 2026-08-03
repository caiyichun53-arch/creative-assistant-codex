from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os
import subprocess
from typing import Any


class SingleInstanceAlreadyRunning(RuntimeError):
    pass


def hidden_process_kwargs() -> dict[str, Any]:
    """Keep controlled child processes out of the Windows desktop."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


@contextmanager
def single_instance_guard(name: str):
    """Keep one Windows desktop service instance alive without stale lock files."""
    if os.name != "nt":
        yield
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool

    handle = create_mutex(None, False, rf"Local\{name}")
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "could not create the single-instance guard")
    if ctypes.get_last_error() == 183:
        close_handle(handle)
        raise SingleInstanceAlreadyRunning(name)

    try:
        yield
    finally:
        close_handle(handle)
