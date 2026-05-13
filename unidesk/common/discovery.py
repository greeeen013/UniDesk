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


def _lan_broadcast_addresses() -> list[str]:
    """Return subnet broadcast addresses for all active LAN interfaces.

    Uses the connect-to-external trick: a UDP socket picks the outbound interface
    without actually sending anything. Falls back to 255.255.255.255.
    """
    addrs: list[str] = []
    # Collect all IPv4 addresses from getaddrinfo (covers multi-homed machines)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip: str = info[4][0]
            if not ip.startswith("127."):
                prefix = ip.rsplit(".", 1)[0]
                bc = prefix + ".255"
                if bc not in addrs:
                    addrs.append(bc)
    except OSError:
        pass
    # Also add the interface that has a default route (most reliable single pick)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))   # no data sent — just picks the route
            local_ip: str = s.getsockname()[0]
        prefix = local_ip.rsplit(".", 1)[0]
        bc = prefix + ".255"
        if bc not in addrs:
            addrs.append(bc)
    except OSError:
        pass
    if not addrs:
        addrs.append("255.255.255.255")
    return addrs


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
            targets = _lan_broadcast_addresses()
            log.debug("Discovery broadcast → %s", targets)
            for bc in targets:
                try:
                    sock.sendto(payload, (bc, UDP_DISCOVERY_PORT))
                except OSError as exc:
                    log.warning("Discovery broadcast to %s failed: %s", bc, exc)
            time.sleep(_BROADCAST_INTERVAL)
        sock.close()


def discover_server(timeout: float = 8.0) -> Optional[tuple[str, int]]:
    """Listen for a server broadcast. Returns (ip, tcp_port) or None on timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", UDP_DISCOVERY_PORT))
    log.info("Listening for server broadcast on UDP :%d (timeout %.0fs)…", UDP_DISCOVERY_PORT, timeout)
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
