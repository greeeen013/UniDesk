"""
Clipboard sync on the client side.
Mirrors ClipboardServer logic: listens for WM_CLIPBOARDUPDATE
and calls *on_change(text)* when local clipboard changes.
Also exposes write() to apply clipboard received from server.
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


class ClipboardClient:
    def __init__(self, on_change: Callable[[str], None]) -> None:
        self._on_change = on_change
        self._last_text: Optional[str] = None
        self._suppress_next = False
        self._hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._message_loop,
            name="clipboard-client",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)

    def write(self, text: str) -> None:
        """Apply clipboard text received from server."""
        self._suppress_next = True
        self._set_clipboard_text(text)
        self._last_text = text

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # ------------------------------------------------------------------
        # API Signatures (Critical for 64-bit compatibility)
        # ------------------------------------------------------------------
        HWND = ctypes.wintypes.HWND
        LPARAM = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LRESULT = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, ctypes.c_uint, WPARAM, LPARAM)

        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [HWND, ctypes.c_uint, WPARAM, LPARAM]

        user32.RegisterClassExW.restype = ctypes.wintypes.ATOM
        user32.CreateWindowExW.restype = HWND
        user32.CreateWindowExW.argtypes = [
            ctypes.wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, 
            ctypes.wintypes.DWORD, ctypes.c_int, ctypes.c_int, 
            ctypes.c_int, ctypes.c_int, HWND, ctypes.wintypes.HMENU, 
            ctypes.wintypes.HINSTANCE, ctypes.c_void_p
        ]

        user32.AddClipboardFormatListener.restype = ctypes.wintypes.BOOL
        user32.AddClipboardFormatListener.argtypes = [HWND]

        user32.RemoveClipboardFormatListener.restype = ctypes.wintypes.BOOL
        user32.RemoveClipboardFormatListener.argtypes = [HWND]

        user32.GetMessageW.restype = ctypes.wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), HWND, ctypes.c_uint, ctypes.c_uint]

        user32.TranslateMessage.restype = ctypes.wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]

        user32.DispatchMessageW.restype = LRESULT
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]

        user32.PostQuitMessage.restype = None
        user32.PostQuitMessage.argtypes = [ctypes.c_int]

        # ------------------------------------------------------------------
        # Window Class and Procedure
        # ------------------------------------------------------------------

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                self._handle_update()
                return 0
            if msg == WM_DESTROY:
                user32.RemoveClipboardFormatListener(hwnd)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = WNDPROC(wnd_proc)

        class WNDCLASSEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HINSTANCE),
                ("hIcon", ctypes.wintypes.HICON),
                ("hCursor", ctypes.wintypes.HANDLE),
                ("hbrBackground", ctypes.wintypes.HBRUSH),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.wintypes.HICON),
            ]

        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]

        h_inst = kernel32.GetModuleHandleW(None)
        class_name = "UniDeskClipboardClient"

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = self._wnd_proc_cb
        wc.hInstance = h_inst
        wc.lpszClassName = class_name
        
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            pass

        HWND_MESSAGE = HWND(-3)
        hwnd = user32.CreateWindowExW(
            0, class_name, "UniDesk Clipboard Client", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, h_inst, None,
        )
        if not hwnd:
            log.error("Failed to create clipboard client window (Error: %s)", ctypes.GetLastError())
            return

        self._hwnd = hwnd
        if not user32.AddClipboardFormatListener(hwnd):
            log.error("AddClipboardFormatListener failed (Error: %s)", ctypes.GetLastError())
            return

        log.info("Clipboard client listener started successfully")

        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_update(self) -> None:
        if self._suppress_next:
            self._suppress_next = False
            log.debug("Suppressing clipboard update (write origin)")
            return
        text = self._get_clipboard_text()
        if text is not None and text != self._last_text:
            self._last_text = text
            log.debug("Clipboard changed (%d chars)", len(text))
            try:
                self._on_change(text)
            except Exception as exc:
                log.warning("Clipboard callback error: %s", exc)

    def _get_clipboard_text(self) -> Optional[str]:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        
        # Signatures
        HWND = ctypes.wintypes.HWND
        user32.OpenClipboard.restype = ctypes.wintypes.BOOL
        user32.OpenClipboard.argtypes = [HWND]
        user32.CloseClipboard.restype = ctypes.wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
        kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HANDLE]

        text = None
        try:
            if not user32.OpenClipboard(None):
                return None
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    text = ctypes.wstring_at(ptr)
                    kernel32.GlobalUnlock(h)
        except Exception as exc:
            log.warning("GetClipboardData error: %s", exc)
        finally:
            user32.CloseClipboard()
        return text

    def _set_clipboard_text(self, text: str) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        
        # Signatures
        user32.OpenClipboard.restype = ctypes.wintypes.BOOL
        user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
        user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.wintypes.HANDLE]
        kernel32.GlobalAlloc.restype = ctypes.wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
        kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HANDLE]

        try:
            if not user32.OpenClipboard(None):
                return
            user32.EmptyClipboard()
            encoded = (text + "\x00").encode("utf-16-le")
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    ctypes.memmove(ptr, encoded, len(encoded))
                    kernel32.GlobalUnlock(h)
                    user32.SetClipboardData(CF_UNICODETEXT, h)
        except Exception as exc:
            log.warning("SetClipboardData error: %s", exc)
        finally:
            user32.CloseClipboard()
