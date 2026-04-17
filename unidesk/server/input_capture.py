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
from typing import Optional

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

SPI_SETCURSORS = 0x0057
# All standard system cursor IDs — replaced with a blank cursor when hiding
_SYSTEM_CURSOR_IDS = [
    32512,  # OCR_NORMAL
    32513,  # OCR_IBEAM
    32514,  # OCR_WAIT
    32515,  # OCR_CROSS
    32516,  # OCR_UP
    32642,  # OCR_SIZENWSE
    32643,  # OCR_SIZENESW
    32644,  # OCR_SIZEWE
    32645,  # OCR_SIZENS
    32646,  # OCR_SIZEALL
    32648,  # OCR_NO
    32649,  # OCR_HAND
    32650,  # OCR_APPSTARTING
]

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

# SendInput structures — used by set_cursor_pos so the resulting WM_MOUSEMOVE has
# LLMHF_INJECTED set, which our hook skips automatically (no _repositioning flag needed).
INPUT_MOUSE            = 0
MOUSEEVENTF_MOVE       = 0x0001
MOUSEEVENTF_ABSOLUTE   = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000   # coordinates span entire virtual desktop
SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type",   ctypes.wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


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
        # CallNextHookEx: LRESULT (pointer-sized) return, HHOOK + nCode + WPARAM + LPARAM args.
        # argtypes must be set — without them ctypes defaults to c_int for the lParam (64-bit
        # pointer), which raises OverflowError on every hook invocation on 64-bit Windows.
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,   # hhk  (HHOOK — pointer-sized, can be NULL)
            ctypes.c_int,      # nCode
            ctypes.c_size_t,   # wParam (WPARAM — unsigned pointer-sized)
            ctypes.c_ssize_t,  # lParam (LPARAM — signed pointer-sized)
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        # WH_MOUSE_LL / WH_KEYBOARD_LL run in the installing thread — hMod must be NULL.
        # Passing GetModuleHandleW(None) with default c_int restype truncates the 64-bit
        # handle to 32 bits → error 126 (ERROR_MOD_NOT_FOUND). Use None (NULL) instead.
        self._mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._mouse_cb, None, 0
        )
        if not self._mouse_hook:
            err = ctypes.windll.kernel32.GetLastError()
            log.error("Failed to install WH_MOUSE_LL hook (error %d)", err)

        self._keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._keyboard_cb, None, 0
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

        # Skip injected events — this covers our own set_cursor_pos (SendInput) calls
        # as well as any other synthetic input we generate.
        if ms.flags & LLMHF_INJECTED:
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
        """Move the server cursor via SendInput.

        SendInput sets LLMHF_INJECTED on the resulting WM_MOUSEMOVE, which our hook
        skips automatically — no recursion, no _repositioning race condition.
        (SetCursorPos does NOT set LLMHF_INJECTED and its WM_MOUSEMOVE was often
        processed after _repositioning was already cleared, causing spurious releases.)
        """
        user32 = ctypes.windll.user32
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        norm_x = int((x - vx) * 65535 / (vw - 1)) if vw > 1 else 0
        norm_y = int((y - vy) * 65535 / (vh - 1)) if vh > 1 else 0
        inp = INPUT(
            type=INPUT_MOUSE,
            _input=_INPUT_UNION(mi=MOUSEINPUT(
                dx=norm_x,
                dy=norm_y,
                mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                time=0,
                dwExtraInfo=None,
            )),
        )
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def show_cursor(self, visible: bool) -> None:
        user32 = ctypes.windll.user32
        if visible:
            # Reload all system cursors from the current cursor scheme in the registry.
            # This is the documented way to undo SetSystemCursor replacements.
            user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
        else:
            # ShowCursor is per-thread on Windows — calling it from a non-UI thread has
            # no effect on windows owned by other threads. Instead, replace every system
            # cursor with an invisible 32×32 cursor; SetSystemCursor is process-agnostic.
            and_mask = (ctypes.c_ubyte * 128)(*([0xFF] * 128))
            xor_mask = (ctypes.c_ubyte * 128)(*([0x00] * 128))
            for cursor_id in _SYSTEM_CURSOR_IDS:
                blank = user32.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
                if blank:
                    user32.SetSystemCursor(blank, cursor_id)
