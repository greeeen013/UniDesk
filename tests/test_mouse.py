import ctypes
import ctypes.wintypes
import time
import math

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
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

def get_pos():
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def set_pos_old(x, y):
    user32 = ctypes.windll.user32
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    norm_x = math.ceil((x - vx) * 65536 / vw) if vw else 0
    norm_y = math.ceil((y - vy) * 65536 / vh) if vh else 0
    inp = INPUT(
        type=INPUT_MOUSE,
        _input=_INPUT_UNION(mi=MOUSEINPUT(
            dx=norm_x, dy=norm_y, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0, dwExtraInfo=None,
        )),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def set_pos_new(x, y):
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
            dx=norm_x, dy=norm_y, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0, dwExtraInfo=None,
        )),
    )
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

print("Current POS:", get_pos())
target_x, target_y = 2800, 540
print("Testing OLD set_pos to", target_x, target_y)
set_pos_old(target_x, target_y)
time.sleep(0.1)
print("After OLD:", get_pos())

target_x, target_y = 2880, 540
print("Testing NEW set_pos to", target_x, target_y)
set_pos_new(target_x, target_y)
time.sleep(0.1)
print("After NEW:", get_pos())
