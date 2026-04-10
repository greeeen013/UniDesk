"""
Low-level keyboard and mouse capture via Win32 WH_MOUSE_LL and WH_KEYBOARD_LL hooks.

IMPORTANT: This module must run on a thread that owns a Win32 message pump
(PeekMessage/GetMessage loop). The hook callbacks return quickly — they only
enqueue events and check a fast flag for suppression.

Admin rights are NOT required for low-level hooks installed by a GUI process
(one that has a visible window or a message loop). However, the hooks will NOT
receive events from processes running at higher integrity levels (e.g., UAC
dialogs, Task Manager). Run as Administrator to capture those.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import queue
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Win32 constants
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

PM_REMOVE = 0x0001
HC_ACTION = 0

_BUTTON_MAP = {
    WM_LBUTTONDOWN: ("left", "press"),
    WM_LBUTTONUP: ("left", "release"),
    WM_RBUTTONDOWN: ("right", "press"),
    WM_RBUTTONUP: ("right", "release"),
    WM_MBUTTONDOWN: ("middle", "press"),
    WM_MBUTTONUP: ("middle", "release"),
}


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


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


# On 64-bit Windows, LRESULT and LPARAM are pointer-sized (8 bytes).
# ctypes.wintypes.LPARAM is c_long (4 bytes) — wrong on 64-bit.
# Use c_ssize_t (signed pointer-sized) for LRESULT/LPARAM, c_size_t for WPARAM.
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,  # LRESULT
    ctypes.c_int,      # nCode
    ctypes.c_size_t,   # wParam (WPARAM)
    ctypes.c_ssize_t,  # lParam (LPARAM — pointer to MSLLHOOKSTRUCT/KBDLLHOOKSTRUCT)
)

# Injected event flag — used to avoid re-processing events we injected ourselves
LLMHF_INJECTED = 0x00000001


class InputCapture:
    """
    Installs WH_MOUSE_LL and WH_KEYBOARD_LL hooks.
    Publishes parsed events to self.event_queue.

    Event format (dict):
        mouse_move:   {"kind": "mouse_move", "x": int, "y": int}
        mouse_button: {"kind": "mouse_button", "button": str, "action": str, "x": int, "y": int}
        mouse_scroll: {"kind": "mouse_scroll", "dx": int, "dy": int, "x": int, "y": int}
        key:          {"kind": "key", "vk": int, "scan": int, "action": str, "flags": int}
    """

    def __init__(self) -> None:
        self.event_queue: queue.Queue = queue.Queue()
        # When True, mouse + keyboard events are suppressed (not passed to OS)
        self.is_forwarding: bool = False
        # When True, the next cursor move is ours (SetCursorPos) — skip it
        self._repositioning: bool = False

        self._mouse_hook: Optional[ctypes.wintypes.HHOOK] = None
        self._keyboard_hook: Optional[ctypes.wintypes.HHOOK] = None
        self._hook_thread: Optional[threading.Thread] = None
        # Keep ctypes references alive
        self._mouse_cb = HOOKPROC(self._mouse_proc)
        self._keyboard_cb = HOOKPROC(self._keyboard_proc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Install hooks in a dedicated thread with a message pump."""
        self._hook_thread = threading.Thread(
            target=self._hook_thread_main,
            name="input-hook",
            daemon=True,
        )
        self._hook_thread.start()

    def stop(self) -> None:
        if self._hook_thread and self._hook_thread.is_alive():
            # Post WM_QUIT to the hook thread's message loop
            ctypes.windll.user32.PostThreadMessageW(
                self._hook_thread.ident, 0x0012, 0, 0  # WM_QUIT
            )

    # ------------------------------------------------------------------
    # Hook thread
    # ------------------------------------------------------------------

    def _hook_thread_main(self) -> None:
        user32 = ctypes.windll.user32
        # SetWindowsHookExW returns HHOOK (pointer-sized handle).
        # Default restype is c_int (32-bit) which truncates the handle on 64-bit Windows.
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        # CallNextHookEx also returns LRESULT (pointer-sized).
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        h_inst = ctypes.windll.kernel32.GetModuleHandleW(None)

        self._mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_cb, h_inst, 0
        )
        if not self._mouse_hook:
            err = ctypes.windll.kernel32.GetLastError()
            log.error("Failed to install WH_MOUSE_LL hook (error %d)", err)

        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_cb, h_inst, 0
        )
        if not self._keyboard_hook:
            err = ctypes.windll.kernel32.GetLastError()
            log.error("Failed to install WH_KEYBOARD_LL hook (error %d)", err)

        log.info("Input hooks installed")

        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

        if self._mouse_hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._mouse_hook)
        if self._keyboard_hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._keyboard_hook)
        log.info("Input hooks removed")

    # ------------------------------------------------------------------
    # Hook callbacks  (called on the hook thread — must be FAST)
    # ------------------------------------------------------------------

    def _mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < HC_ACTION:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents

        # Skip injected events (e.g. from our own SendInput on server)
        if ms.flags & LLMHF_INJECTED:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        # Skip our own cursor repositioning
        if self._repositioning:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        x, y = ms.pt.x, ms.pt.y

        if w_param == WM_MOUSEMOVE:
            self.event_queue.put({"kind": "mouse_move", "x": x, "y": y})

        elif w_param in _BUTTON_MAP:
            button, action = _BUTTON_MAP[w_param]
            self.event_queue.put({"kind": "mouse_button", "button": button, "action": action, "x": x, "y": y})

        elif w_param in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            hi = ms.mouseData >> 16
            button = "x1" if hi == XBUTTON1 else "x2"
            action = "press" if w_param == WM_XBUTTONDOWN else "release"
            self.event_queue.put({"kind": "mouse_button", "button": button, "action": action, "x": x, "y": y})

        elif w_param == WM_MOUSEWHEEL:
            delta = ctypes.c_short(ms.mouseData >> 16).value
            self.event_queue.put({"kind": "mouse_scroll", "dx": 0, "dy": delta, "x": x, "y": y})

        elif w_param == WM_MOUSEHWHEEL:
            delta = ctypes.c_short(ms.mouseData >> 16).value
            self.event_queue.put({"kind": "mouse_scroll", "dx": delta, "dy": 0, "x": x, "y": y})

        # Suppress event if we are currently forwarding to a client
        if self.is_forwarding:
            return 1   # non-zero = suppress
        return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _keyboard_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code < HC_ACTION:
            return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

        kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

        action = "press" if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) else "release"
        self.event_queue.put({
            "kind": "key",
            "vk": kb.vkCode,
            "scan": kb.scanCode,
            "action": action,
            "flags": kb.flags,
        })

        if self.is_forwarding:
            return 1
        return ctypes.windll.user32.CallNextHookEx(None, n_code, w_param, l_param)

    # ------------------------------------------------------------------
    # Cursor helpers (call from any thread)
    # ------------------------------------------------------------------

    def set_cursor_pos(self, x: int, y: int) -> None:
        """Move the server cursor. Sets repositioning flag to skip the resulting event."""
        self._repositioning = True
        ctypes.windll.user32.SetCursorPos(x, y)
        self._repositioning = False

    def show_cursor(self, visible: bool) -> None:
        if visible:
            while ctypes.windll.user32.ShowCursor(True) < 0:
                pass
        else:
            while ctypes.windll.user32.ShowCursor(False) >= 0:
                pass
