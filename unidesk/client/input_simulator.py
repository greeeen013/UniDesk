"""
Input simulation on the client side using Win32 SendInput.

SendInput is lower-level than pynput and injects events with hardware flags,
making them indistinguishable from real input for most applications.

Admin rights are NOT required for SendInput as long as you are targeting
your own desktop session. UAC-elevated windows will block injected input
unless the client also runs elevated.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Win32 structs
# ------------------------------------------------------------------

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


def _send_input(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _get_virtual_desktop() -> tuple[int, int, int, int]:
    gm = ctypes.windll.user32.GetSystemMetrics
    return (
        gm(SM_XVIRTUALSCREEN),
        gm(SM_YVIRTUALSCREEN),
        gm(SM_CXVIRTUALSCREEN),
        gm(SM_CYVIRTUALSCREEN),
    )


# ------------------------------------------------------------------
# Public simulator classes
# ------------------------------------------------------------------

class MouseSimulator:
    def move_absolute(self, x: int, y: int) -> None:
        """Move cursor to absolute position (in client monitor coords)."""
        vx, vy, vw, vh = _get_virtual_desktop()
        # Normalize to [0, 65535] over the full virtual desktop
        norm_x = int((x - vx) * 65535 / vw) if vw else 0
        norm_y = int((y - vy) * 65535 / vh) if vh else 0
        inp = _INPUT(
            type=INPUT_MOUSE,
            _input=_INPUT_UNION(mi=_MOUSEINPUT(
                dx=norm_x, dy=norm_y,
                dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            )),
        )
        _send_input(inp)

    def button(self, button: str, action: str) -> None:
        flag_map = {
            ("left", "press"):    MOUSEEVENTF_LEFTDOWN,
            ("left", "release"):  MOUSEEVENTF_LEFTUP,
            ("right", "press"):   MOUSEEVENTF_RIGHTDOWN,
            ("right", "release"): MOUSEEVENTF_RIGHTUP,
            ("middle", "press"):  MOUSEEVENTF_MIDDLEDOWN,
            ("middle", "release"):MOUSEEVENTF_MIDDLEUP,
        }
        x_data = 0
        if button in ("x1", "x2"):
            flag = MOUSEEVENTF_XDOWN if action == "press" else MOUSEEVENTF_XUP
            x_data = XBUTTON1 if button == "x1" else XBUTTON2
        else:
            flag = flag_map.get((button, action))
            if not flag:
                return
        inp = _INPUT(
            type=INPUT_MOUSE,
            _input=_INPUT_UNION(mi=_MOUSEINPUT(dwFlags=flag, mouseData=x_data)),
        )
        _send_input(inp)

    def scroll(self, dx: int, dy: int) -> None:
        if dy:
            inp = _INPUT(
                type=INPUT_MOUSE,
                _input=_INPUT_UNION(mi=_MOUSEINPUT(
                    mouseData=ctypes.c_ulong(dy).value,
                    dwFlags=MOUSEEVENTF_WHEEL,
                )),
            )
            _send_input(inp)
        if dx:
            inp = _INPUT(
                type=INPUT_MOUSE,
                _input=_INPUT_UNION(mi=_MOUSEINPUT(
                    mouseData=ctypes.c_ulong(dx).value,
                    dwFlags=MOUSEEVENTF_HWHEEL,
                )),
            )
            _send_input(inp)


class KeyboardSimulator:
    def key_event(self, vk: int, scan: int, action: str, flags: int = 0) -> None:
        # Use VK code as primary identifier (NOT KEYEVENTF_SCANCODE).
        # This ensures system shortcuts (Alt+Tab, Win+D, Alt+F4, ...) are
        # processed correctly by the Windows shell — scan-code-only injection
        # can confuse the task switcher on some Windows versions.
        # wScan is still included as a hint for apps that inspect it.
        # LLKHF_EXTENDED (0x01) in hook flags == KEYEVENTF_EXTENDEDKEY (0x01),
        # so the bit-mask check is intentional.
        key_flags = 0
        if action == "release":
            key_flags |= KEYEVENTF_KEYUP
        if flags & KEYEVENTF_EXTENDEDKEY:   # LLKHF_EXTENDED shares bit 0
            key_flags |= KEYEVENTF_EXTENDEDKEY
        inp = _INPUT(
            type=INPUT_KEYBOARD,
            _input=_INPUT_UNION(ki=_KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=key_flags,
            )),
        )
        _send_input(inp)
