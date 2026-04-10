"""
Clipboard monitoring on the server side.

Uses Win32 AddClipboardFormatListener (via a hidden message-only window)
to receive WM_CLIPBOARDUPDATE notifications without polling.

When the clipboard changes, the new text is pushed to all connected clients
via the provided callback.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardServer:
    """
    Monitors clipboard changes and calls *on_change(text)* when the content
    changes. Ignores changes triggered by our own writes (anti-loop).
    """

    def __init__(self, on_change: Callable[[str], None]) -> None:
        self._on_change = on_change
        self._last_text: Optional[str] = None
        self._suppress_next = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._message_loop,
            name="clipboard-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    def write(self, text: str) -> None:
        """Write *text* to the clipboard without triggering our own listener."""
        self._suppress_next = True
        self._set_clipboard_text(text)
        self._last_text = text

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _message_loop(self) -> None:
        # On 64-bit Windows LRESULT/LPARAM/WPARAM are all 8 bytes (pointer-sized).
        # ctypes.wintypes.LPARAM is c_long (4 bytes) — must use c_ssize_t instead.
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,      # LRESULT
            ctypes.wintypes.HWND,  # hwnd
            ctypes.c_uint,         # msg  (UINT — 32-bit, correct)
            ctypes.c_size_t,       # wParam (WPARAM — pointer-sized unsigned)
            ctypes.c_ssize_t,      # lParam (LPARAM — pointer-sized signed)
        )

        user32 = ctypes.windll.user32
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
        ]

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                self._handle_update()
                return 0
            if msg == WM_DESTROY:
                user32.RemoveClipboardFormatListener(hwnd)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_cb = WNDPROC(wnd_proc)

        WNDCLASSEX = type("WNDCLASSEX", (ctypes.Structure,), {
            "_fields_": [
                ("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HANDLE),
                ("hIcon", ctypes.wintypes.HANDLE),
                ("hCursor", ctypes.wintypes.HANDLE),
                ("hbrBackground", ctypes.wintypes.HANDLE),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.wintypes.HANDLE),
            ]
        })

        h_inst = ctypes.windll.kernel32.GetModuleHandleW(None)
        class_name = "UniDeskClipboard"

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = h_inst
        wc.lpszClassName = class_name

        ctypes.windll.user32.RegisterClassExW(ctypes.byref(wc))

        HWND_MESSAGE = ctypes.wintypes.HWND(-3)
        hwnd = ctypes.windll.user32.CreateWindowExW(
            0, class_name, "UniDesk Clipboard", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, h_inst, None,
        )
        if not hwnd:
            log.error("Failed to create clipboard listener window")
            return

        self._hwnd = hwnd
        ctypes.windll.user32.AddClipboardFormatListener(hwnd)
        log.info("Clipboard listener started")

        msg = ctypes.wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_update(self) -> None:
        if self._suppress_next:
            self._suppress_next = False
            return
        text = self._get_clipboard_text()
        if text and text != self._last_text:
            self._last_text = text
            log.debug("Clipboard changed (%d chars)", len(text))
            try:
                self._on_change(text)
            except Exception as exc:
                log.warning("Clipboard callback error: %s", exc)

    def _get_clipboard_text(self) -> Optional[str]:
        try:
            if not ctypes.windll.user32.OpenClipboard(None):
                return None
            h = ctypes.windll.user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return None
            ptr = ctypes.windll.kernel32.GlobalLock(h)
            if not ptr:
                return None
            text = ctypes.wstring_at(ptr)
            ctypes.windll.kernel32.GlobalUnlock(h)
            return text
        except Exception as exc:
            log.warning("GetClipboardData error: %s", exc)
            return None
        finally:
            ctypes.windll.user32.CloseClipboard()

    def _set_clipboard_text(self, text: str) -> None:
        try:
            if not ctypes.windll.user32.OpenClipboard(None):
                return
            ctypes.windll.user32.EmptyClipboard()
            encoded = (text + "\x00").encode("utf-16-le")
            h = ctypes.windll.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if h:
                ptr = ctypes.windll.kernel32.GlobalLock(h)
                ctypes.memmove(ptr, encoded, len(encoded))
                ctypes.windll.kernel32.GlobalUnlock(h)
                ctypes.windll.user32.SetClipboardData(CF_UNICODETEXT, h)
        except Exception as exc:
            log.warning("SetClipboardData error: %s", exc)
        finally:
            ctypes.windll.user32.CloseClipboard()
