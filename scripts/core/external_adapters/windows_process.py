from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
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


@contextmanager
def blocking_process_mutex(name: str):
    """Hold one cross-process slot with the operating system's blocking primitive."""
    if os.name != "nt":
        import fcntl

        lock_path = Path(tempfile.gettempdir()) / (
            "creation_assistant_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24] + ".lock"
        )
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    wait_for_single_object.restype = ctypes.c_uint32
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (ctypes.c_void_p,)
    release_mutex.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool

    handle = create_mutex(None, False, rf"Local\{name}")
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "could not create the blocking process mutex")
    result = wait_for_single_object(handle, 0xFFFFFFFF)
    if result not in {0, 0x80}:
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(error, f"could not acquire the blocking process mutex: {result}")
    try:
        yield
    finally:
        release_mutex(handle)
        close_handle(handle)
