import ctypes
import ctypes.wintypes

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

try:
    m1 = MOUSEINPUT(dx=1, dy=2, mouseData=0, dwFlags=0, time=0)
    print("m1 success. dwExtraInfo:", m1.dwExtraInfo)
except Exception as e:
    print("m1 error:", e)

try:
    m2 = MOUSEINPUT(dx=1, dy=2, mouseData=0, dwFlags=0, time=0, dwExtraInfo=None)
    print("m2 success. dwExtraInfo:", m2.dwExtraInfo)
except Exception as e:
    print("m2 error:", e)

try:
    m3 = MOUSEINPUT(dx=1, dy=2, mouseData=0, dwFlags=0, time=0, dwExtraInfo=ctypes.cast(0, ctypes.POINTER(ctypes.c_ulong)))
    print("m3 success. dwExtraInfo:", m3.dwExtraInfo)
except Exception as e:
    print("m3 error:", e)
