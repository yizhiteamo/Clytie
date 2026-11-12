"""RSS of the explicitly supplied measurement process identity, never discovery."""

import ctypes
import os
from pathlib import Path

from clytie.plugin_host.locking import process_identity


def file_bytes(root, *, suffix=""):
    """Sample a moving fictional tree; only proven disappearance is benign."""

    def onerror(error):
        if not isinstance(error, FileNotFoundError):
            raise error

    total = 0
    for directory, _, names in os.walk(root, onerror=onerror, followlinks=False):
        for name in names:
            if not name.endswith(suffix):
                continue
            try:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError("measurement_resource_link")
                total += path.stat().st_size
            except FileNotFoundError:
                pass  # publication/retirement completed after enumeration
    return total


def rss_bytes(identity):
    if (
        type(identity) is not dict
        or set(identity) != {"pid", "started_at"}
        or type(identity["pid"]) is not int
        or identity["pid"] <= 0
    ):
        raise ValueError("measurement_process_invalid")
    pid = identity["pid"]
    if process_identity(pid) != identity:
        raise ValueError("measurement_process_identity_changed")
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak",
                    "working",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000 | 0x10, False, pid)
        if not handle:
            raise OSError("measurement_rss_unavailable")
        try:
            counter = Counters()
            counter.cb = ctypes.sizeof(counter)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(Counters),
                wintypes.DWORD,
            ]
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counter), counter.cb):
                raise OSError("measurement_rss_unavailable")
            value = counter.working
        finally:
            kernel.CloseHandle(handle)
    else:
        value = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    if process_identity(pid) != identity:
        raise ValueError("measurement_process_identity_changed")
    return value
