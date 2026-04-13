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


if __name__ == "__main__":
    test_make_clipboard_push_image_dib()
    test_make_clipboard_push_image_png()
    test_make_clipboard_push_text_unchanged()
    print("All protocol tests passed!")
