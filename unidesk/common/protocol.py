"""
Network protocol for UniDesk.

Each message is a length-prefixed JSON packet:
  [4 bytes: uint32 big-endian message length][N bytes: UTF-8 JSON body]

All message dicts must contain a "type" field (see constants.MsgType).
"""

from __future__ import annotations

import json
import socket
import struct
import time
from typing import Any

from .constants import MsgType, APP_VERSION


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------

HEADER_FMT = ">I"   # big-endian unsigned int (4 bytes)
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def encode_message(payload: dict) -> bytes:
    """Serialize *payload* to a framed bytes object ready to be sent."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = struct.pack(HEADER_FMT, len(body))
    return header + body


def send_message(sock: socket.socket, payload: dict) -> None:
    """Send a framed message over *sock*. Raises OSError on failure."""
    data = encode_message(payload)
    sock.sendall(data)


def recv_message(sock: socket.socket) -> dict:
    """
    Receive one framed message from *sock*.
    Blocks until a complete message arrives.
    Raises:
        ConnectionError  – if the peer closed the connection
        json.JSONDecodeError – if the body is not valid JSON
        OSError – on socket errors
    """
    header = _recv_exactly(sock, HEADER_SIZE)
    if not header:
        raise ConnectionError("Connection closed by peer")
    (length,) = struct.unpack(HEADER_FMT, header)
    body = _recv_exactly(sock, length)
    if not body:
        raise ConnectionError("Connection closed mid-message")
    return json.loads(body.decode("utf-8"))


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, accumulating fragments."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------

def make_handshake_req(hostname: str) -> dict:
    return {"type": MsgType.HANDSHAKE_REQ, "version": APP_VERSION, "hostname": hostname}


def make_handshake_ack(client_id: str, server_monitors: list[dict]) -> dict:
    return {
        "type": MsgType.HANDSHAKE_ACK,
        "version": APP_VERSION,
        "client_id": client_id,
        "server_monitors": server_monitors,
    }


def make_monitor_info(monitors: list[dict]) -> dict:
    return {"type": MsgType.MONITOR_INFO, "monitors": monitors}


def make_mouse_move(x: int, y: int) -> dict:
    return {"type": MsgType.MOUSE_MOVE, "x": x, "y": y}


def make_mouse_button(button: str, action: str) -> dict:
    """button: 'left'|'right'|'middle'|'x1'|'x2'  action: 'press'|'release'"""
    return {"type": MsgType.MOUSE_BUTTON, "button": button, "action": action}


def make_mouse_scroll(dx: int, dy: int) -> dict:
    return {"type": MsgType.MOUSE_SCROLL, "dx": dx, "dy": dy}


def make_key_event(vk: int, scan: int, action: str, flags: int = 0) -> dict:
    """action: 'press'|'release'"""
    return {"type": MsgType.KEY_EVENT, "vk": vk, "scan": scan, "action": action, "flags": flags}


def make_clipboard_push(text: str) -> dict:
    return {"type": MsgType.CLIPBOARD_PUSH, "format": "text", "data": text}


def make_control_grant() -> dict:
    return {"type": MsgType.CONTROL_GRANT}


def make_control_release() -> dict:
    return {"type": MsgType.CONTROL_RELEASE}


def make_control_release_request() -> dict:
    return {"type": MsgType.CONTROL_RELEASE_REQUEST}


def make_ping() -> dict:
    return {"type": MsgType.PING, "ts": time.time()}


def make_pong(ping_ts: float) -> dict:
    return {"type": MsgType.PONG, "ts": time.time(), "ping_ts": ping_ts}


def make_error(message: str) -> dict:
    return {"type": MsgType.ERROR, "message": message}
