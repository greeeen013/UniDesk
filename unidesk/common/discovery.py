"""UDP broadcast discovery for UniDesk."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Optional

from .constants import UDP_DISCOVERY_PORT

log = logging.getLogger(__name__)

_MAGIC = "UniDesk-v1"
_BROADCAST_INTERVAL = 2.0


class DiscoveryServer:
    """Broadcasts server presence via UDP on the local network."""

    def __init__(self, tcp_port: int) -> None:
        self._tcp_port = tcp_port
        self._running = False

    def start(self) -> None:
        self._running = True
        t = threading.Thread(target=self._loop, name="discovery-bc", daemon=True)
        t.start()
        log.info("Discovery broadcaster started (UDP :%d)", UDP_DISCOVERY_PORT)

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        payload = json.dumps({
            "magic": _MAGIC,
            "port": self._tcp_port,
            "hostname": socket.gethostname(),
        }).encode()
        while self._running:
            try:
                sock.sendto(payload, ("255.255.255.255", UDP_DISCOVERY_PORT))
            except OSError as exc:
                log.warning("Discovery broadcast error: %s", exc)
            time.sleep(_BROADCAST_INTERVAL)
        sock.close()


def discover_server(timeout: float = 8.0) -> Optional[tuple[str, int]]:
    """Listen for a server broadcast. Returns (ip, tcp_port) or None on timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", UDP_DISCOVERY_PORT))
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                return None
            try:
                msg = json.loads(data.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("magic") == _MAGIC:
                port = int(msg.get("port", UDP_DISCOVERY_PORT - 1))
                log.info("Discovered server at %s (hostname=%s, port=%d)", addr[0], msg.get("hostname", "?"), port)
                return addr[0], port
    finally:
        sock.close()
