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
import os
import struct
import tempfile
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002

CF_UNICODETEXT = 13
CF_DIB = 8
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

# Limits sized to finish within HEARTBEAT_TIMEOUT (10 s) even on slow WiFi.
# Proper fix for large files is a dedicated transfer channel (like audio uses).
_FILE_SIZE_LIMIT  = 5  * 1024 * 1024   # 5 MB per file
_FILE_TOTAL_LIMIT = 10 * 1024 * 1024   # 10 MB across all files in one batch

_TEMP_DIR = os.path.join(tempfile.gettempdir(), "UniDesk_clipboard")


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
        self._last_files_sig: frozenset = frozenset()
        self._suppress_count: int = 0
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
        self._suppress_count += 1
        fmt = payload.get("format")
        if fmt == "text":
            self._set_clipboard_text(payload.get("data", ""))
            self._last_text = payload.get("data", "")
        elif fmt == "image":
            data = base64.b64decode(payload["data"])
            encoding = payload.get("encoding", "dib+b64")
            if encoding == "png+b64":
                dib = _png_to_dib(data)
                if dib is None:
                    log.warning("Skipping clipboard write: cannot convert png+b64 without Pillow")
                    self._suppress_count -= 1
                    return
                data = dib
            self._set_clipboard_image(data)
            self._last_image_hash = hashlib.md5(data).hexdigest()
        elif fmt == "files":
            files = payload.get("files", [])
            if not files:
                self._suppress_count -= 1
                return
            temp_paths = _write_files_to_temp(files)
            if not temp_paths:
                self._suppress_count -= 1
                return
            self._set_clipboard_files(temp_paths)
            self._last_files_sig = _files_signature(temp_paths)
            log.debug("Clipboard written: %d file(s) from network", len(temp_paths))

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

        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
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
        if self._suppress_count > 0:
            self._suppress_count -= 1
            log.debug("Suppressing clipboard update (write origin)")
            return

        # Text takes priority
        text = self._get_clipboard_text()
        if text is not None and text != self._last_text:
            self._last_text = text
            self._last_image_hash = None
            self._last_files_sig = frozenset()
            log.debug("Clipboard changed: text (%d chars)", len(text))
            try:
                from ..common.protocol import make_clipboard_push
                self._on_change(make_clipboard_push(text))
            except Exception as exc:
                log.warning("Clipboard callback error: %s", exc)
            return

        # Files (CF_HDROP) — checked before image so directories don't fall through to DIB
        file_paths = self._get_clipboard_files()
        if file_paths is not None:
            sig = _files_signature(file_paths)
            if sig and sig != self._last_files_sig:
                self._last_files_sig = sig
                self._last_text = None
                self._last_image_hash = None
                log.debug("Clipboard changed: %d file(s)", len(file_paths))
                try:
                    files_data = _read_files(file_paths)
                    if files_data:
                        from ..common.protocol import make_clipboard_push_files
                        self._on_change(make_clipboard_push_files(files_data))
                except Exception as exc:
                    log.warning("Clipboard files callback error: %s", exc)
            return

        # Image fallback
        dib = self._get_clipboard_image()
        if dib is not None:
            h = hashlib.md5(dib).hexdigest()
            if h == self._last_image_hash:
                return
            self._last_image_hash = h
            self._last_text = None
            self._last_files_sig = frozenset()
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

    def _get_clipboard_files(self) -> Optional[list[str]]:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32

        user32.OpenClipboard.restype = ctypes.wintypes.BOOL
        user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
        user32.CloseClipboard.restype = ctypes.wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        shell32.DragQueryFileW.restype = ctypes.c_uint
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]

        paths: list[str] = []
        try:
            if not user32.OpenClipboard(None):
                return None
            h = user32.GetClipboardData(CF_HDROP)
            if not h:
                return None
            count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            for i in range(count):
                buf = ctypes.create_unicode_buffer(32768)
                if shell32.DragQueryFileW(h, i, buf, 32768) > 0:
                    paths.append(buf.value)
        except Exception as exc:
            log.warning("GetClipboardData(CF_HDROP) error: %s", exc)
            paths = []
        finally:
            user32.CloseClipboard()
        return paths if paths else None

    def _set_clipboard_files(self, paths: list[str]) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.restype = ctypes.wintypes.BOOL
        user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
        user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.wintypes.HANDLE]
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        kernel32.GlobalAlloc.restype = ctypes.wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
        kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HANDLE]

        # DROPFILES header (20 bytes): pFiles=20, pt=(0,0), fNC=0, fWide=1
        file_list_bytes = ("".join(p + "\x00" for p in paths) + "\x00").encode("utf-16-le")
        hdr = struct.pack("<IIIII", 20, 0, 0, 0, 1)
        data = hdr + file_list_bytes

        # "Preferred DropEffect" = DROPEFFECT_COPY (5) so Explorer copies, not moves, temp files
        cf_drop_effect = user32.RegisterClipboardFormatW("Preferred DropEffect")
        drop_effect = struct.pack("<I", 5)

        try:
            if not user32.OpenClipboard(None):
                return
            user32.EmptyClipboard()

            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if h:
                ptr = kernel32.GlobalLock(h)
                if ptr:
                    try:
                        ctypes.memmove(ptr, data, len(data))
                        user32.SetClipboardData(CF_HDROP, h)
                    finally:
                        kernel32.GlobalUnlock(h)

            if cf_drop_effect:
                h2 = kernel32.GlobalAlloc(GMEM_MOVEABLE, 4)
                if h2:
                    ptr2 = kernel32.GlobalLock(h2)
                    if ptr2:
                        try:
                            ctypes.memmove(ptr2, drop_effect, 4)
                            user32.SetClipboardData(cf_drop_effect, h2)
                        finally:
                            kernel32.GlobalUnlock(h2)
        except Exception as exc:
            log.warning("SetClipboardData(CF_HDROP) error: %s", exc)
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
# File clipboard helpers
# ---------------------------------------------------------------------------

def _files_signature(paths: list[str]) -> frozenset:
    sig = set()
    for p in paths:
        try:
            if os.path.isfile(p):
                st = os.stat(p)
                sig.add((p, st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    return frozenset(sig)


def _read_files(paths: list[str]) -> list[dict]:
    result: list[dict] = []
    total = 0
    for path in paths:
        if not os.path.isfile(path):
            log.debug("Skipping non-file clipboard entry: %s", path)
            continue
        size = os.path.getsize(path)
        if size > _FILE_SIZE_LIMIT:
            log.warning(
                "Skipping file too large for clipboard sync (%d MB): %s",
                size // (1024 * 1024), os.path.basename(path),
            )
            continue
        if total + size > _FILE_TOTAL_LIMIT:
            log.warning("Clipboard file batch limit reached — skipping remaining files")
            break
        with open(path, "rb") as f:
            data = f.read()
        result.append({"name": os.path.basename(path), "data": base64.b64encode(data).decode("ascii")})
        total += size
    return result


def _write_files_to_temp(files: list[dict]) -> list[str]:
    os.makedirs(_TEMP_DIR, exist_ok=True)
    for name in os.listdir(_TEMP_DIR):
        try:
            os.remove(os.path.join(_TEMP_DIR, name))
        except OSError:
            pass
    paths: list[str] = []
    for file_info in files:
        safe_name = os.path.basename(file_info.get("name", ""))
        if not safe_name:
            continue
        dest = os.path.join(_TEMP_DIR, safe_name)
        try:
            raw = base64.b64decode(file_info["data"])
            with open(dest, "wb") as f:
                f.write(raw)
            paths.append(dest)
        except Exception as exc:
            log.warning("Failed to write temp file %s: %s", safe_name, exc)
    return paths


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


def _png_to_dib(png: bytes) -> Optional[bytes]:
    """Convert PNG bytes to raw CF_DIB bytes. Returns None if Pillow missing."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        buf = io.BytesIO()
        img.save(buf, format='BMP')
        return buf.getvalue()[14:]  # strip 14-byte BITMAPFILEHEADER
    except ImportError:
        log.warning("Pillow not installed — cannot decode png+b64 image, skipping")
        return None
    except Exception as exc:
        log.warning("PNG→DIB conversion failed: %s", exc)
        return png
