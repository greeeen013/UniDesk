"""
Clipboard monitoring on the server side.

Uses Win32 AddClipboardFormatListener (via a hidden message-only window)
to receive WM_CLIPBOARDUPDATE notifications without polling.

When the clipboard changes, a CLIPBOARD_PUSH payload dict is passed to
the provided on_change(payload) callback.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


class ClipboardServer:
    """
    Monitors clipboard changes and calls *on_change(payload)* when the content
    changes. payload is a ready-to-send CLIPBOARD_PUSH dict.
    Ignores changes triggered by our own writes (anti-loop).
    """

    def __init__(self, on_change: Callable[[dict], None], compress_images: bool = False) -> None:
        self._on_change = on_change
        self._compress_images = compress_images
        self._last_text: Optional[str] = None
        self._last_image_hash: Optional[str] = None
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

    def write(self, payload: dict) -> None:
        """Write clipboard content received from a client without triggering our own listener."""
        self._suppress_next = True
        fmt = payload.get("format")
        if fmt == "text":
            self._set_clipboard_text(payload.get("data", ""))
            self._last_text = payload.get("data", "")
        elif fmt == "image":
            data = base64.b64decode(payload["data"])
            encoding = payload.get("encoding", "dib+b64")
            if encoding == "png+b64":
                data = _png_to_dib(data)
            self._set_clipboard_image(data)
            self._last_image_hash = hashlib.md5(data).hexdigest()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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
        class_name = "UniDeskClipboard"

        wc = WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(WNDCLASSEX)
        wc.lpfnWndProc = self._wnd_proc_cb
        wc.hInstance = h_inst
        wc.lpszClassName = class_name

        if not user32.RegisterClassExW(ctypes.byref(wc)):
            pass

        HWND_MESSAGE = HWND(-3)
        hwnd = user32.CreateWindowExW(
            0, class_name, "UniDesk Clipboard", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, h_inst, None,
        )
        if not hwnd:
            log.error("Failed to create clipboard listener window (Error: %s)", ctypes.GetLastError())
            return

        self._hwnd = hwnd
        if not user32.AddClipboardFormatListener(hwnd):
            log.error("AddClipboardFormatListener failed (Error: %s)", ctypes.GetLastError())
            return

        log.info("Clipboard listener started successfully")

        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_update(self) -> None:
        if self._suppress_next:
            self._suppress_next = False
            log.debug("Suppressing clipboard update (write origin)")
            return

        # Text takes priority
        text = self._get_clipboard_text()
        if text is not None and text != self._last_text:
            self._last_text = text
            self._last_image_hash = None
            log.debug("Clipboard changed: text (%d chars)", len(text))
            try:
                from ..common.protocol import make_clipboard_push
                self._on_change(make_clipboard_push(text))
            except Exception as exc:
                log.warning("Clipboard callback error: %s", exc)
            return

        # Image fallback
        dib = self._get_clipboard_image()
        if dib is not None:
            h = hashlib.md5(dib).hexdigest()
            if h == self._last_image_hash:
                return
            self._last_image_hash = h
            self._last_text = None
            log.debug("Clipboard changed: image (%d bytes DIB)", len(dib))
            try:
                from ..common.protocol import make_clipboard_push_image
                if self._compress_images:
                    png = _dib_to_png(dib)
                    if png is not None:
                        payload = make_clipboard_push_image(png, encoding="png+b64")
                    else:
                        payload = make_clipboard_push_image(dib, encoding="dib+b64")
                else:
                    payload = make_clipboard_push_image(dib, encoding="dib+b64")
                self._on_change(payload)
            except Exception as exc:
                log.warning("Clipboard image callback error: %s", exc)

    def _get_clipboard_text(self) -> Optional[str]:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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
            log.warning("GetClipboardData(text) error: %s", exc)
        finally:
            user32.CloseClipboard()
        return text

    def _get_clipboard_image(self) -> Optional[bytes]:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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
        kernel32.GlobalSize.restype = ctypes.c_size_t
        kernel32.GlobalSize.argtypes = [ctypes.wintypes.HANDLE]

        data = None
        try:
            if not user32.OpenClipboard(None):
                return None
            h = user32.GetClipboardData(CF_DIB)
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    size = kernel32.GlobalSize(h)
                    data = (ctypes.c_char * size).from_address(ptr).raw
                    kernel32.GlobalUnlock(h)
        except Exception as exc:
            log.warning("GetClipboardData(CF_DIB) error: %s", exc)
        finally:
            user32.CloseClipboard()
        return data

    def _set_clipboard_text(self, text: str) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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
                    try:
                        ctypes.memmove(ptr, encoded, len(encoded))
                        user32.SetClipboardData(CF_UNICODETEXT, h)
                    finally:
                        kernel32.GlobalUnlock(h)
        except Exception as exc:
            log.warning("SetClipboardData(text) error: %s", exc)
        finally:
            user32.CloseClipboard()

    def _set_clipboard_image(self, dib: bytes) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    try:
                        ctypes.memmove(ptr, dib, len(dib))
                        user32.SetClipboardData(CF_DIB, h)
                    finally:
                        kernel32.GlobalUnlock(h)
        except Exception as exc:
            log.warning("SetClipboardData(CF_DIB) error: %s", exc)
        finally:
            user32.CloseClipboard()


# ---------------------------------------------------------------------------
# Pillow-based conversion helpers (used only with --compress-images)
# ---------------------------------------------------------------------------

def _dib_to_png(dib: bytes) -> Optional[bytes]:
    """Convert raw CF_DIB bytes to PNG bytes. Returns None if Pillow missing."""
    try:
        import struct, io
        from PIL import Image
        header_size = struct.unpack_from('<I', dib, 0)[0]
        bit_count = struct.unpack_from('<H', dib, 14)[0]
        clr_used = struct.unpack_from('<I', dib, 32)[0]
        num_colors = clr_used if clr_used > 0 else ((1 << bit_count) if bit_count <= 8 else 0)
        pixel_offset = 14 + header_size + num_colors * 4
        file_size = 14 + len(dib)
        file_header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, pixel_offset)
        img = Image.open(io.BytesIO(file_header + dib))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except ImportError:
        log.warning("--compress-images requires Pillow: pip install Pillow")
        return None
    except Exception as exc:
        log.warning("DIB→PNG conversion failed: %s", exc)
        return None


def _png_to_dib(png: bytes) -> bytes:
    """Convert PNG bytes to raw CF_DIB bytes. Falls back gracefully if Pillow missing."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        buf = io.BytesIO()
        img.save(buf, format='BMP')
        return buf.getvalue()[14:]  # strip 14-byte BITMAPFILEHEADER
    except ImportError:
        log.warning("Pillow not installed — cannot decode png+b64 image, skipping")
        return png
    except Exception as exc:
        log.warning("PNG→DIB conversion failed: %s", exc)
        return png
