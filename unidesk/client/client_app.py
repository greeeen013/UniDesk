"""
Client application — connects to UniDesk server and handles input forwarding.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import socket
import threading
import time
from typing import Optional

from ..common import protocol as proto
from ..common.config import MonitorRect
from ..common.constants import TCP_PORT, MsgType, HEARTBEAT_INTERVAL
from ..common.discovery import discover_server
from .audio_client import AudioClient
from .clipboard_client import ClipboardClient
from .cursor_manager import CursorManager
from .input_simulator import MouseSimulator, KeyboardSimulator
from .monitor_info_client import get_monitors

log = logging.getLogger(__name__)


class ClientApp:
    def __init__(self, server_host: Optional[str], port: int = TCP_PORT, hide_mouse: bool = False, compress_images: bool = False) -> None:
        self.server_host = server_host  # None = auto-discover
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._send_queue: queue.Queue = queue.Queue()
        self._mouse = MouseSimulator()
        self._keyboard = KeyboardSimulator()
        self._cursor = CursorManager(on_grab_request=self._on_local_grab, hide_mouse=hide_mouse)
        self._clipboard = ClipboardClient(on_change=self._on_local_clipboard, compress_images=compress_images)
        self._audio: Optional[AudioClient] = None
        self.client_id: Optional[str] = None
        self.connected_host: Optional[str] = None  # set after successful connection
        self.server_monitors: list[MonitorRect] = []

        # Callbacks for GUI / tray
        self.on_connected: Optional[callable] = None
        self.on_disconnected: Optional[callable] = None

    def start(self) -> None:
        ctypes.windll.user32.SetProcessDPIAware()
        self._running = True
        self._clipboard.start()
        t = threading.Thread(target=self._connect_loop, name="client-connect", daemon=True)
        t.start()

    def stop(self) -> None:
        self._running = False
        self._clipboard.stop()
        if self._audio:
            self._audio.stop()
        self._send_queue.put(None)  # unblock writer
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect_loop(self) -> None:
        """Attempt to connect, reconnect on failure."""
        while self._running:
            try:
                host = self.server_host
                port = self.port
                if host is None:
                    log.info("Auto-discovering server on local network...")
                    result = discover_server()
                    if result is None:
                        log.warning("No server found — retrying in 5s")
                        time.sleep(5)
                        continue
                    host, port = result
                self._connect(host, port)
            except Exception as exc:
                log.warning("Connection error: %s — retrying in 5s", exc)
                time.sleep(5)

    def _connect(self, host: str, port: int) -> None:
        log.info("Connecting to %s:%d", host, port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.connect((host, port))
        self._sock = sock
        self.connected_host = host

        import socket as _socket
        hostname = _socket.gethostname()
        proto.send_message(sock, proto.make_handshake_req(hostname))
        ack = proto.recv_message(sock)
        if ack.get("type") != MsgType.HANDSHAKE_ACK:
            raise ConnectionError("Unexpected handshake response")

        self.client_id = ack["client_id"]
        self.server_monitors = [MonitorRect.from_dict(m) for m in ack.get("server_monitors", [])]
        log.info("Connected — client_id=%s", self.client_id)

        # Send our monitor info
        my_monitors = get_monitors()
        proto.send_message(sock, proto.make_monitor_info([m.to_dict() for m in my_monitors]))

        # Start audio stream after handshake (client_id now known)
        self._audio = AudioClient.from_control_port(
            host=host,
            control_port=port,
            client_id=self.client_id,
        )
        self._audio.start()

        if self.on_connected:
            self.on_connected()

        writer = threading.Thread(target=self._writer_loop, name="client-writer", daemon=True)
        writer.start()

        # Heartbeat
        hb = threading.Thread(target=self._heartbeat_loop, name="client-hb", daemon=True)
        hb.start()

        self._reader_loop()   # blocks until disconnected

    def _reader_loop(self) -> None:
        while self._running and self._sock:
            try:
                msg = proto.recv_message(self._sock)
                self._dispatch(msg)
            except ConnectionError:
                break
            except Exception as exc:
                log.warning("Reader error: %s", exc)
                break
        log.info("Disconnected from server")
        self._cursor.release_control()
        if self._audio:
            self._audio.stop()
            self._audio = None
        if self.on_disconnected:
            self.on_disconnected()

    def _writer_loop(self) -> None:
        while self._running and self._sock:
            try:
                msg = self._send_queue.get(timeout=1.0)
                if msg is None:
                    break
                proto.send_message(self._sock, msg)
            except queue.Empty:
                continue
            except OSError:
                break

    def _heartbeat_loop(self) -> None:
        while self._running and self._sock:
            time.sleep(HEARTBEAT_INTERVAL)
            self._send_queue.put(proto.make_ping())

    def _send(self, msg: dict) -> None:
        self._send_queue.put(msg)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")

        if t == MsgType.MOUSE_MOVE:
            self._mouse.move_absolute(msg["x"], msg["y"])

        elif t == MsgType.MOUSE_BUTTON:
            self._mouse.button(msg["button"], msg["action"])

        elif t == MsgType.MOUSE_SCROLL:
            self._mouse.scroll(msg["dx"], msg["dy"])

        elif t == MsgType.KEY_EVENT:
            self._keyboard.key_event(msg["vk"], msg["scan"], msg["action"], msg.get("flags", 0))

        elif t == MsgType.CONTROL_GRANT:
            self._cursor.grant_control()

        elif t == MsgType.CONTROL_RELEASE:
            self._cursor.release_control()

        elif t == MsgType.CLIPBOARD_PUSH:
            self._clipboard.write(msg)

        elif t == MsgType.PING:
            self._send(proto.make_pong(msg.get("ts", 0)))

        elif t == MsgType.PONG:
            pass  # heartbeat ack

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_local_grab(self) -> None:
        """User physically moved mouse — request control back."""
        log.info("Sending CONTROL_RELEASE_REQUEST")
        self._send(proto.make_control_release_request())

    def _on_local_clipboard(self, payload: dict) -> None:
        """Local clipboard changed — push to server."""
        self._send(payload)
