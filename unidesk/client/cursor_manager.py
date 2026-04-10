"""
Cursor and local input management on the client.

When the server grants control:
  - Local physical mouse is suppressed via WH_MOUSE_LL hook
  - Cursor is hidden
  - If user physically moves mouse past threshold → send CONTROL_RELEASE_REQUEST

When control is released:
  - Hook is removed
  - Cursor is shown
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
from typing import Callable, Optional

from ..common.constants import LOCAL_MOUSE_GRAB_THRESHOLD

log = logging.getLogger(__name__)

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
HC_ACTION = 0
LLMHF_INJECTED = 0x00000001

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT
    ctypes.c_int,      # nCode
    ctypes.c_size_t,   # wParam
    ctypes.c_ssize_t,  # lParam (pointer to MSLLHOOKSTRUCT)
)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class CursorManager:
    """
    Manages local cursor visibility and suppression when server has control.
    Calls *on_grab_request()* when the local user physically moves the mouse.
    """

    def __init__(self, on_grab_request: Callable[[], None]) -> None:
        self._on_grab_request = on_grab_request
        self._remote_controlled = False
        self._hook: Optional[ctypes.wintypes.HHOOK] = None
        self._hook_cb = HOOKPROC(self._mouse_proc)
        self._last_x: Optional[int] = None
        self._last_y: Optional[int] = None
        self._hook_thread: Optional[threading.Thread] = None

    def grant_control(self) -> None:
        """Server is now controlling this client."""
        self._remote_controlled = True
        self._show_cursor(False)
        self._install_hook()
        log.info("Remote control granted — local mouse suppressed")

    def release_control(self) -> None:
        """Server released control; restore local input."""
        self._remote_controlled = False
        self._remove_hook()
        self._show_cursor(True)
        self._last_x = None
        self._last_y = None
        log.info("Remote control released — local mouse restored")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _install_hook(self) -> None:
        if self._hook:
            return
        self._hook_thread = threading.Thread(
            target=self._hook_thread_main, name="cursor-hook", daemon=True
        )
        self._hook_thread.start()

    def _remove_hook(self) -> None:
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._hook_thread:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._hook_thread.ident, 0x0012, 0, 0  # WM_QUIT
                )
            except Exception:
                pass

    def _hook_thread_main(self) -> None:
        user32 = ctypes.windll.user32
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        h_inst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._hook_cb, h_inst, 0
        )
        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def _mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < HC_ACTION:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

        # Allow injected events (from server's SendInput) through
        if ms.flags & LLMHF_INJECTED:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        if w_param == WM_MOUSEMOVE and self._remote_controlled:
            x, y = ms.pt.x, ms.pt.y
            if self._last_x is not None:
                delta = abs(x - self._last_x) + abs(y - self._last_y)
                if delta >= LOCAL_MOUSE_GRAB_THRESHOLD:
                    log.info("Physical mouse grab detected (delta=%d)", delta)
                    try:
                        self._on_grab_request()
                    except Exception:
                        pass
            self._last_x = x
            self._last_y = y
            return 1  # suppress physical mouse while server controls

        return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _show_cursor(self, visible: bool) -> None:
        if visible:
            while ctypes.windll.user32.ShowCursor(True) < 0:
                pass
        else:
            while ctypes.windll.user32.ShowCursor(False) >= 0:
                pass
