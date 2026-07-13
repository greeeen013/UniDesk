"""
Screen capture client — grabs one of this PC's monitors and streams JPEG
frames over a dedicated TCP connection to the server, on demand (while a
view window is open on PC1).

Mirrors audio_client.py's connection pattern: one small JSON header, then a
stream of length-prefixed binary chunks (JPEG frames instead of PCM).
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from typing import Optional

from ..common.config import MonitorRect
from ..common.constants import SCREEN_CAPTURE_FPS, SCREEN_JPEG_QUALITY, SCREEN_PORT_OFFSET

log = logging.getLogger(__name__)

try:
    import mss
    import numpy as np
    import cv2
    _CAPTURE_AVAILABLE = True
except ImportError:
    mss = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    cv2 = None  # type: ignore[assignment]
    _CAPTURE_AVAILABLE = False


class ScreenCaptureSession:
    """Captures one monitor and streams JPEG frames to the server while running."""

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        session_id: str,
        monitor_index: int,
        monitor: MonitorRect,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._session_id = session_id
        self._monitor_index = monitor_index
        self._monitor = monitor
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_control_port(
        cls,
        host: str,
        control_port: int,
        client_id: str,
        session_id: str,
        monitor_index: int,
        monitor: MonitorRect,
    ) -> "ScreenCaptureSession":
        return cls(
            host=host,
            port=control_port + SCREEN_PORT_OFFSET,
            client_id=client_id,
            session_id=session_id,
            monitor_index=monitor_index,
            monitor=monitor,
        )

    def start(self) -> None:
        if not _CAPTURE_AVAILABLE:
            log.warning(
                "mss/opencv not installed — screen view disabled. "
                "Run: pip install mss opencv-python"
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="screen-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._session()
        except Exception as exc:
            if self._running:
                log.warning("Screen capture session error: %s", exc)

    def _session(self) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((self._host, self._port))

            mon = self._monitor
            header = json.dumps({
                "client_id": self._client_id,
                "session_id": self._session_id,
                "monitor_index": self._monitor_index,
                "width": mon.width,
                "height": mon.height,
            }).encode()
            sock.sendall(struct.pack(">I", len(header)) + header)

            log.info(
                "Screen capture connected to %s:%d (monitor %d, %dx%d)",
                self._host, self._port, self._monitor_index, mon.width, mon.height,
            )

            region = {"left": mon.left, "top": mon.top, "width": mon.width, "height": mon.height}
            interval = 1.0 / SCREEN_CAPTURE_FPS

            with mss.mss() as sct:
                while self._running:
                    frame_start = time.monotonic()

                    shot = sct.grab(region)
                    frame = np.array(shot)  # BGRA
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, SCREEN_JPEG_QUALITY]
                    )
                    if ok:
                        data = buf.tobytes()
                        sock.sendall(struct.pack(">I", len(data)) + data)

                    elapsed = time.monotonic() - frame_start
                    time.sleep(max(0.0, interval - elapsed))

        except (OSError, BrokenPipeError) as exc:
            if self._running:
                log.debug("Screen capture socket closed: %s", exc)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            log.info("Screen capture session %s ended", self._session_id)
