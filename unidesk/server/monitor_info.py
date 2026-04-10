"""
Monitor enumeration using Win32 API.
Returns physical pixel coordinates (requires DPI awareness declared at startup).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes

from ..common.config import MonitorRect


def get_monitors() -> list[MonitorRect]:
    """Return all connected monitors as a list of MonitorRect (physical pixels)."""
    monitors: list[MonitorRect] = []
    monitor_id = 0

    def _callback(h_monitor, hdc, lp_rect, dw_data):
        nonlocal monitor_id
        info = _MONITORINFOEX()
        info.cbSize = ctypes.sizeof(_MONITORINFOEX)
        if ctypes.windll.user32.GetMonitorInfoW(h_monitor, ctypes.byref(info)):
            rc = info.rcMonitor
            monitors.append(MonitorRect(
                id=monitor_id,
                left=rc.left,
                top=rc.top,
                right=rc.right,
                bottom=rc.bottom,
                is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                name=info.szDevice,
            ))
            monitor_id += 1
        return True

    cb = _MONITORENUMPROC(_callback)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)
    return monitors


def get_virtual_desktop_rect() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the combined virtual desktop."""
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    gm = ctypes.windll.user32.GetSystemMetrics
    return (
        gm(SM_XVIRTUALSCREEN),
        gm(SM_YVIRTUALSCREEN),
        gm(SM_CXVIRTUALSCREEN),
        gm(SM_CYVIRTUALSCREEN),
    )


# ---------------------------------------------------------------------------
# Win32 structs
# ---------------------------------------------------------------------------

MONITORINFOF_PRIMARY = 0x00000001

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.wintypes.HMONITOR,
    ctypes.wintypes.HDC,
    ctypes.POINTER(_RECT),
    ctypes.wintypes.LPARAM,
)
