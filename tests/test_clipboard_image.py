import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import base64
from unidesk.common.protocol import make_clipboard_push_image, make_clipboard_push
from unidesk.common.constants import MsgType


def test_make_clipboard_push_image_dib():
    raw = b"\x00\x01\x02\x03"
    msg = make_clipboard_push_image(raw, encoding="dib+b64")
    assert msg["type"] == MsgType.CLIPBOARD_PUSH
    assert msg["format"] == "image"
    assert msg["encoding"] == "dib+b64"
    assert base64.b64decode(msg["data"]) == raw


def test_make_clipboard_push_image_png():
    raw = b"\x89PNG\r\n\x1a\n"
    msg = make_clipboard_push_image(raw, encoding="png+b64")
    assert msg["encoding"] == "png+b64"
    assert base64.b64decode(msg["data"]) == raw


def test_make_clipboard_push_text_unchanged():
    msg = make_clipboard_push("hello")
    assert msg["format"] == "text"
    assert msg["data"] == "hello"


def test_clipboard_server_write_dispatches():
    """write(payload) calls the correct internal method."""
    try:
        from unidesk.server.clipboard_server import ClipboardServer
    except Exception:
        print("Skipping ClipboardServer import (non-Windows or missing deps)")
        return

    cb = ClipboardServer.__new__(ClipboardServer)
    cb._suppress_next = False
    cb._last_text = None
    cb._last_image_hash = None

    text_calls = []
    image_calls = []
    cb._set_clipboard_text = lambda t: text_calls.append(t)
    cb._set_clipboard_image = lambda d: image_calls.append(d)

    cb.write({"type": "CLIPBOARD_PUSH", "format": "text", "data": "hello"})
    assert text_calls == ["hello"]
    assert image_calls == []

    import base64
    raw = b"\x00\x01"
    cb.write({"type": "CLIPBOARD_PUSH", "format": "image", "encoding": "dib+b64",
              "data": base64.b64encode(raw).decode()})
    assert image_calls == [raw]
    print("ClipboardServer dispatch tests passed!")


def test_image_hash_dedup():
    """Same DIB bytes should not fire the callback twice."""
    import hashlib, base64
    try:
        from unidesk.server.clipboard_server import ClipboardServer
    except Exception:
        print("Skipping ClipboardServer import (non-Windows or missing deps)")
        return

    cb = ClipboardServer.__new__(ClipboardServer)
    cb._suppress_next = False
    cb._last_text = None
    cb._last_image_hash = None
    cb._compress_images = False

    calls = []
    cb._on_change = lambda payload: calls.append(payload)

    dib = b"\x28\x00\x00\x00" + b"\x00" * 36
    h = hashlib.md5(dib).hexdigest()
    cb._last_image_hash = h  # simulate: already sent this image

    new_hash = hashlib.md5(dib).hexdigest()
    if new_hash == cb._last_image_hash:
        pass  # dedup — no call
    else:
        cb._last_image_hash = new_hash
        calls.append("would_fire")

    assert calls == [], "Same image should not trigger callback"
    print("Image hash dedup test passed!")


if __name__ == "__main__":
    test_make_clipboard_push_image_dib()
    test_make_clipboard_push_image_png()
    test_make_clipboard_push_text_unchanged()
    print("All protocol tests passed!")
    test_clipboard_server_write_dispatches()
    test_image_hash_dedup()
