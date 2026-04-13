"""
Main server application.

Orchestrates:
- TCP server (accept connections)
- InputCapture (WH_MOUSE_LL / WH_KEYBOARD_LL hooks)
- EdgeDetector (boundary → client routing)
- ClientManager (track connections)
- ClipboardServer (clipboard sync)

Call ServerApp.run() — it blocks until stopped.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import selectors
import socket
import threading
import time
from typing import Optional

from ..common import protocol as proto
from ..common.config import MonitorRect, VirtualPlacement
from ..common.constants import TCP_PORT, MsgType, HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT
from .client_manager import ClientManager, ConnectedClient
from .clipboard_server import ClipboardServer
from .edge_detector import EdgeDetector
from .input_capture import InputCapture
from .monitor_info import get_monitors, get_virtual_desktop_rect

log = logging.getLogger(__name__)


class ServerApp:
    def __init__(self, port: int = TCP_PORT, sensitivity: float = 1.0, scale_to_snap: bool = False, hide_mouse: bool = False, compress_images: bool = False) -> None:
        self.port = port
        self.sensitivity = sensitivity
        self.scale_to_snap = scale_to_snap
        self.hide_mouse = hide_mouse
        self._monitors: list[MonitorRect] = []
        self._client_mgr = ClientManager()
        self._edge = EdgeDetector([], scale_to_snap=scale_to_snap)
        self._capture = InputCapture()
        self._clipboard = ClipboardServer(on_change=self._on_clipboard_change, compress_images=compress_images)
        self._sel = selectors.DefaultSelector()
        self._active_client_id: Optional[str] = None
        self._running = False

        # Virtual cursor tracking: physical cursor is locked at boundary while forwarding,
        # so absolute coords can't accumulate. We track a virtual position by summing deltas
        # from each event, then translate that to client coords.
        self._virt_x: float = 0.0   # virtual position in server-space coords
        self._virt_y: float = 0.0
        self._last_raw_x: int = 0   # server cursor position at last event / last warp
        self._last_raw_y: int = 0

        # Callbacks for GUI updates
        self.on_client_connected: Optional[callable] = None
        self.on_client_disconnected: Optional[callable] = None
        self.on_monitors_changed: Optional[callable] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all subsystems in background threads. Non-blocking."""
        # Make process DPI-aware so monitor coords are in physical pixels
        ctypes.windll.user32.SetProcessDPIAware()

        self._monitors = get_monitors()
        self._edge.update_server_monitors(self._monitors)
        log.info("Server monitors: %s", self._monitors)

        self._capture.start()
        self._clipboard.start()

        self._running = True
        self._event_thread = threading.Thread(
            target=self._event_loop, name="event-loop", daemon=True
        )
        self._event_thread.start()

        self._network_thread = threading.Thread(
            target=self._network_loop, name="network-loop", daemon=True
        )
        self._network_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

        log.info("Server started on port %d", self.port)

    def stop(self) -> None:
        self._running = False
        self._capture.stop()
        self._clipboard.stop()

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _network_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.port))
        srv.listen(10)
        srv.setblocking(False)
        self._sel.register(srv, selectors.EVENT_READ, data=None)
        log.info("Listening on :%d", self.port)

        while self._running:
            events = self._sel.select(timeout=1.0)
            for key, mask in events:
                if key.data is None:
                    # New connection
                    conn, addr = srv.accept()
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.setblocking(True)   # reader thread uses blocking recv
                    self._handle_new_connection(conn, addr)
                else:
                    # Incoming data from existing client
                    client: ConnectedClient = key.data
                    self._read_from_client(client)

        srv.close()

    def _handle_new_connection(self, conn: socket.socket, addr: tuple) -> None:
        """Do handshake synchronously, then register client in selector."""
        try:
            conn.settimeout(5.0)
            msg = proto.recv_message(conn)
            if msg.get("type") != MsgType.HANDSHAKE_REQ:
                conn.close()
                return
            hostname = msg.get("hostname", addr[0])
            client = self._client_mgr.add(conn, hostname)
            proto.send_message(conn, proto.make_handshake_ack(
                client_id=client.client_id,
                server_monitors=[m.to_dict() for m in self._monitors],
            ))
            conn.settimeout(None)
            conn.setblocking(False)
            self._sel.register(conn, selectors.EVENT_READ, data=client)
            if self.on_client_connected:
                self.on_client_connected(client)
        except Exception as exc:
            log.warning("Handshake failed from %s: %s", addr, exc)
            conn.close()

    def _read_from_client(self, client: ConnectedClient) -> None:
        try:
            client.conn.setblocking(True)
            msg = proto.recv_message(client.conn)
            client.conn.setblocking(False)
            self._dispatch_client_message(client, msg)
        except ConnectionError:
            self._disconnect_client(client)
        except Exception as exc:
            log.warning("Read error from %s: %s", client.hostname, exc)
            self._disconnect_client(client)

    def _disconnect_client(self, client: ConnectedClient) -> None:
        try:
            self._sel.unregister(client.conn)
        except Exception:
            pass
        if self._active_client_id == client.client_id:
            self._release_control()
        self._edge.remove_client(client.client_id)
        self._client_mgr.remove(client.client_id)
        if self.on_client_disconnected:
            self.on_client_disconnected(client)

    # ------------------------------------------------------------------
    # Client message dispatch
    # ------------------------------------------------------------------

    def _dispatch_client_message(self, client: ConnectedClient, msg: dict) -> None:
        t = msg.get("type")

        if t == MsgType.MONITOR_INFO:
            monitors = [MonitorRect.from_dict(m) for m in msg.get("monitors", [])]
            client.monitors = monitors
            log.info("Client %s reported %d monitor(s)", client.hostname, len(monitors))
            if self.on_client_connected:
                self.on_client_connected(client)  # refresh GUI

        elif t == MsgType.PONG:
            client.last_pong = time.time()

        elif t == MsgType.CONTROL_RELEASE_REQUEST:
            if self._active_client_id == client.client_id:
                self._release_control()
                client.send(proto.make_control_release())

        elif t == MsgType.CLIPBOARD_PUSH:
            self._clipboard.write(msg)

        elif t == MsgType.PING:
            client.send(proto.make_pong(msg.get("ts", 0)))

    # ------------------------------------------------------------------
    # Input event loop
    # ------------------------------------------------------------------

    def _event_loop(self) -> None:
        while self._running:
            try:
                ev = self._capture.event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._handle_input_event(ev)

    def _handle_input_event(self, ev: dict) -> None:
        kind = ev["kind"]

        if kind == "mouse_move":
            x, y = ev["x"], ev["y"]

            if self._active_client_id:
                zone = self._edge.get_zone(self._active_client_id)
                if not zone:
                    return

                # Physical cursor is locked at the zone boundary, so absolute coords
                # can't accumulate. Track delta from the last known cursor position
                # (updated to boundary after each set_cursor_pos warp).
                dx = x - self._last_raw_x
                dy = y - self._last_raw_y

                # Prevent massive jumps from pending events right after warps
                if abs(dx) > 300 or abs(dy) > 300:
                    dx, dy = 0, 0

                # Apply optional hardware DPI synchronization factor
                if self.sensitivity != 1.0:
                    dx *= self.sensitivity
                    dy *= self.sensitivity

                self._virt_x += dx
                self._virt_y += dy
                
                # Prevent virtual cursor from eternally wandering outside the actual dimensions of the client screen,
                # bounded by a 10px buffer to allow edge release triggers to fully actuate.
                self._virt_x = max(zone.rect.left - 10, min(zone.rect.right + 10, self._virt_x))
                self._virt_y = max(zone.rect.top - 10, min(zone.rect.bottom + 10, self._virt_y))

                anchor = self._monitors[zone.placement.anchor_monitor_id]

                # Release: virtual cursor crossed back past zone boundary into server territory
                edge = zone.placement.anchor_edge
                if (
                    (edge == "right"  and self._virt_x < zone.rect.left)
                    or (edge == "left"   and self._virt_x >= zone.rect.right)
                    or (edge == "bottom" and self._virt_y < zone.rect.top)
                    or (edge == "top"    and self._virt_y >= zone.rect.bottom)
                ):
                    log.debug(
                        "Virtual cursor (%.0f, %.0f) left zone %s → releasing",
                        self._virt_x, self._virt_y, zone.client_id,
                    )
                    # Warp physical cursor precisely back to the crossing point
                    ret_x = max(anchor.left, min(anchor.right - 1, int(self._virt_x)))
                    ret_y = max(anchor.top, min(anchor.bottom - 1, int(self._virt_y)))
                    self._capture.set_cursor_pos(ret_x, ret_y)

                    self._release_control()
                    return

                # Translate virtual server-space position to client monitor coords
                cm = zone.client_monitor
                cx = int((self._virt_x - zone.rect.left) / zone.rect.width  * cm.width)
                cy = int((self._virt_y - zone.rect.top)  / zone.rect.height * cm.height)
                cx = max(0, min(cm.width  - 1, cx))
                cy = max(0, min(cm.height - 1, cy))
                
                cx += cm.left
                cy += cm.top

                client = self._client_mgr.get(self._active_client_id)
                if client:
                    client.send(proto.make_mouse_move(cx, cy))

                # Since `_capture.is_forwarding = True`, our hook suppresses the original WM_MOUSEMOVE,
                # meaning the Windows OS cursor is permanently frozen at `_last_raw`.
                # Every new event received natively carries the delta appended to that frozen base!
                # Therefore we do NOT update `_last_raw`; it acts as the eternal origin point.

            else:
                # Check if cursor entered a virtual zone
                result = self._edge.hit_test(x, y)
                if result:
                    client_id, cx, cy = result
                    self._grant_control(client_id, x, y)
                    client = self._client_mgr.get(client_id)
                    if client:
                        client.send(proto.make_mouse_move(cx, cy))

        elif kind == "mouse_button":
            if self._active_client_id:
                client = self._client_mgr.get(self._active_client_id)
                if client:
                    client.send(proto.make_mouse_button(ev["button"], ev["action"]))

        elif kind == "mouse_scroll":
            if self._active_client_id:
                client = self._client_mgr.get(self._active_client_id)
                if client:
                    client.send(proto.make_mouse_scroll(ev["dx"], ev["dy"]))

        elif kind == "key":
            if self._active_client_id:
                client = self._client_mgr.get(self._active_client_id)
                if client:
                    client.send(proto.make_key_event(ev["vk"], ev["scan"], ev["action"], ev["flags"]))

    # ------------------------------------------------------------------
    # Control handoff
    # ------------------------------------------------------------------

    def _grant_control(self, client_id: str, hit_x: int, hit_y: int) -> None:
        self._active_client_id = client_id
        self._capture.is_forwarding = True
        self._capture.show_cursor(False)
        self._virt_x = float(hit_x)
        self._virt_y = float(hit_y)
        
        zone = self._edge.get_zone(client_id)
        if zone:
            anchor = self._monitors[zone.placement.anchor_monitor_id]
            self._last_raw_x = (anchor.left + anchor.right) // 2
            self._last_raw_y = (anchor.top + anchor.bottom) // 2
            self._capture.set_cursor_pos(self._last_raw_x, self._last_raw_y)
        else:
            self._last_raw_x = hit_x
            self._last_raw_y = hit_y
            
        client = self._client_mgr.get(client_id)
        if client:
            client.send(proto.make_control_grant())
        log.info("Control granted to %s", client_id)

        # NOTE: ShowCursor(False) above already hides the cursor visually.
        # We must NOT teleport the cursor elsewhere (e.g. to a corner)
        # because delta tracking relies on the cursor being at the center
        # of the anchor monitor. Windows clamps coordinates at the virtual
        # desktop boundary, so from a corner only up/left deltas are
        # possible — down/right movement is lost entirely.

    def _teleport_to_corner(self) -> None:
        """Teleport server mouse to bottom-right corner of virtual desktop."""
        user32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)
        self._capture.set_cursor_pos(vx + vw - 1, vy + vh - 1)

    def _release_control(self) -> None:
        if self._active_client_id:
            client = self._client_mgr.get(self._active_client_id)
            if client:
                client.send(proto.make_control_release())
        self._active_client_id = None
        self._capture.is_forwarding = False
        self._capture.show_cursor(True)
        log.info("Control returned to server")

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def _on_clipboard_change(self, payload: dict) -> None:
        self._client_mgr.broadcast(payload)

    # ------------------------------------------------------------------
    # Placement API (called by GUI)
    # ------------------------------------------------------------------

    def set_placement(self, placement: VirtualPlacement, client_monitor: MonitorRect) -> None:
        self._edge.update_placement(placement, client_monitor)
        log.info("Placement updated for %s", placement.client_id)

    def get_monitors(self) -> list[MonitorRect]:
        return self._monitors

    def get_clients(self) -> list:
        return self._client_mgr.all_clients()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            for client in self._client_mgr.all_clients():
                if now - client.last_pong > HEARTBEAT_TIMEOUT:
                    log.warning("Client %s timed out", client.hostname)
                    self._disconnect_client(client)
                else:
                    client.send(proto.make_ping())
