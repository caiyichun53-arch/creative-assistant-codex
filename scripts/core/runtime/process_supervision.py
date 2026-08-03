"""OS-level ownership for external worker process trees."""

from __future__ import annotations

import ctypes
import os
import subprocess
from typing import Any


class ProcessTreeControlError(RuntimeError):
    pass


class ManagedProcessTree:
    """Keep a controlled Windows worker and every child it starts in one job.

    Closing the job handle asks Windows to end the whole tree. The same happens
    if the supervising Python process is interrupted, so a crawler cannot leave
    browser or worker children behind after its parent has gone away.
    """

    _KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        create_job.restype = ctypes.c_void_p
        handle = create_job(None, None)
        if not handle:
            raise ProcessTreeControlError(f"could not create controlled process tree: {ctypes.get_last_error()}")

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        set_information.restype = ctypes.c_bool
        if not set_information(handle, self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ProcessTreeControlError(f"could not configure controlled process tree: {error}")
        self._handle = int(handle)

    def add(self, process: subprocess.Popen[Any]) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        assign.restype = ctypes.c_bool
        if not assign(ctypes.c_void_p(self._handle), ctypes.c_void_p(process._handle)):
            raise ProcessTreeControlError(
                f"could not attach worker {process.pid} to controlled process tree: {ctypes.get_last_error()}"
            )

    def close(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))

    def __enter__(self) -> "ManagedProcessTree":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
