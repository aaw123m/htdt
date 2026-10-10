"""Process memory probe — psutil-free, Qt-free (#807 boundary refactor).

``perf_harness`` keeps the benchmark operations (some of which need a
``QApplication`` for widget timing); the RSS probe itself is pure
``ctypes``/``/proc`` plumbing that domain modules may use without pulling in
the UI layer. ``perf_harness`` re-exports it so existing imports keep working.
"""

from __future__ import annotations

import os
import sys


def process_rss_bytes() -> int | None:
    """Resident set size of this process, or None when unprobed."""
    if sys.platform == 'win32':
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            class _MEM_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ('cb', wintypes.DWORD),
                    ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_MEM_COUNTERS),
                wintypes.DWORD,
            ]
            counters = _MEM_COUNTERS()
            counters.cb = ctypes.sizeof(_MEM_COUNTERS)
            if psapi.GetProcessMemoryInfo(
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:  # error-boundary: platform probe — a process-memory probe failure returns unprobed honestly (noqa: BLE001)
            return None
        return None
    try:
        with open('/proc/self/statm', 'r', encoding='utf-8') as handle:
            return int(handle.read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, IndexError, ValueError):
        return None


__all__ = ['process_rss_bytes']
