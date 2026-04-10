"""
Manages connected client sockets.
Each client gets a dedicated writer thread (outbound queue → socket).
The server main loop reads from all client sockets via selectors.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ..common import protocol as proto
from ..common.config import MonitorRect, VirtualPlacement

log = logging.getLogger(__name__)


@dataclass
class ConnectedClient:
    client_id: str
    conn: socket.socket
    hostname: str
    monitors: list[MonitorRect] = field(default_factory=list)
    placement: Optional[VirtualPlacement] = None
    send_queue: queue.Queue = field(default_factory=queue.Queue)
    _writer_thread: Optional[threading.Thread] = field(default=None, repr=False)
    last_pong: float = field(default=0.0)

    def start_writer(self) -> None:
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name=f"writer-{self.client_id[:8]}",
            daemon=True,
        )
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        while True:
            try:
                msg = self.send_queue.get(timeout=1.0)
                if msg is None:   # sentinel → stop
                    break
                proto.send_message(self.conn, msg)
            except queue.Empty:
                continue
            except OSError as exc:
                log.warning("Writer error for %s: %s", self.client_id, exc)
                break

    def send(self, msg: dict) -> None:
        self.send_queue.put(msg)

    def stop(self) -> None:
        self.send_queue.put(None)
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


class ClientManager:
    def __init__(self) -> None:
        self._clients: dict[str, ConnectedClient] = {}
        self._lock = threading.Lock()

    def add(self, conn: socket.socket, hostname: str) -> ConnectedClient:
        client_id = str(uuid.uuid4())
        client = ConnectedClient(
            client_id=client_id,
            conn=conn,
            hostname=hostname,
            last_pong=__import__("time").time(),
        )
        client.start_writer()
        with self._lock:
            self._clients[client_id] = client
        log.info("Client connected: %s (%s)", hostname, client_id)
        return client

    def get(self, client_id: str) -> Optional[ConnectedClient]:
        with self._lock:
            return self._clients.get(client_id)

    def get_by_conn(self, conn: socket.socket) -> Optional[ConnectedClient]:
        with self._lock:
            for c in self._clients.values():
                if c.conn is conn:
                    return c
        return None

    def remove(self, client_id: str) -> None:
        with self._lock:
            client = self._clients.pop(client_id, None)
        if client:
            client.stop()
            log.info("Client disconnected: %s (%s)", client.hostname, client_id)

    def all_clients(self) -> list[ConnectedClient]:
        with self._lock:
            return list(self._clients.values())

    def broadcast(self, msg: dict) -> None:
        for client in self.all_clients():
            client.send(msg)
