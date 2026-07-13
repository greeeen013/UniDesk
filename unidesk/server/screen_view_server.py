"""
Screen view server — receives JPEG frame streams from ScreenCaptureSession
(client, PC2) and forwards each frame to whatever callback is currently
registered for that session (a ScreenViewWindow in the GUI).

Runs on a dedicated TCP port (control_port + SCREEN_PORT_OFFSET), separate
from both the JSON control channel and the audio channel.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
from typing import Callable, Optional

from ..common.constants import SCREEN_PORT_OFFSET

log = logging.getLogger(__name__)

# (jpeg_bytes, width, height) -> None
FrameCallback = Callable[[bytes, int, int], None]


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class ScreenViewServer:
    """Listens for incoming screen-capture streams and dispatches frames by session_id."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callbacks: dict[str, FrameCallback] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_control_port(cls, control_port: int) -> "ScreenViewServer":
        return cls(port=control_port + SCREEN_PORT_OFFSET)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._listen, name="screen-view-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def register(self, session_id: str, callback: FrameCallback) -> None:
        with self._lock:
            self._callbacks[session_id] = callback

    def unregister(self, session_id: str) -> None:
        with self._lock:
            self._callbacks.pop(session_id, None)

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    def _listen(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self._port))
        except OSError as exc:
            log.error("Cannot bind screen view port %d: %s", self._port, exc)
            return
        srv.listen(4)
        srv.settimeout(1.0)
        log.info("Screen view server listening on port %d", self._port)

        while self._running:
            try:
                conn, addr = srv.accept()
                t = threading.Thread(
                    target=self._handle_stream,
                    args=(conn, addr),
                    name=f"screen-recv-{addr[0]}",
                    daemon=True,
                )
                t.start()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    log.warning("Screen view accept error: %s", exc)

        srv.close()

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    def _handle_stream(self, conn: socket.socket, addr: tuple) -> None:
        try:
            raw_len = _recv_exact(conn, 4)
            if not raw_len:
                return
            header_len = struct.unpack(">I", raw_len)[0]
            if header_len > 4096:
                log.warning("Screen view header too large from %s", addr[0])
                return
            header_data = _recv_exact(conn, header_len)
            if not header_data:
                return
            header = json.loads(header_data.decode())

            session_id: str = header["session_id"]
            client_id: str = header.get("client_id", "?")
            width: int = header.get("width", 0)
            height: int = header.get("height", 0)
            log.info(
                "Screen view stream client=%s session=%s %dx%d",
                client_id, session_id, width, height,
            )

            while self._running:
                raw_len = _recv_exact(conn, 4)
                if not raw_len:
                    break
                frame_len = struct.unpack(">I", raw_len)[0]
                if frame_len > 8_000_000:
                    log.warning("Oversized screen frame (%d B) — dropping connection", frame_len)
                    break
                frame = _recv_exact(conn, frame_len)
                if not frame:
                    break

                with self._lock:
                    callback = self._callbacks.get(session_id)
                if callback:
                    callback(frame, width, height)

        except Exception as exc:
            log.warning("Screen view stream error from %s: %s", addr[0], exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            log.info("Screen view stream from %s ended", addr[0])
