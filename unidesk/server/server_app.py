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
import ctypes.wintypes
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
from ..common.discovery import DiscoveryServer
from .audio_server import AudioServer
from .client_manager import ClientManager, ConnectedClient
from .clipboard_server import ClipboardServer
from .edge_detector import EdgeDetector
from .input_capture import InputCapture
from .monitor_info import get_monitors, get_virtual_desktop_rect

log = logging.getLogger(__name__)

_FULLSCREEN_CACHE_TTL = 0.25  # seconds


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork",    ctypes.wintypes.RECT),
        ("dwFlags",   ctypes.wintypes.DWORD),
    ]


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
        self._audio = AudioServer.from_control_port(port)
        self._discovery = DiscoveryServer(tcp_port=port)
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

        # In-memory placement store: hostname → (anchor_monitor_id, anchor_edge, offset_pixels)
        # Persists across client reconnects for the lifetime of the server process.
        self._placement_memory: dict[str, tuple[int, str, int]] = {}

        # Fullscreen detection cache: (monotonic_ts, is_fullscreen)
        self._fullscreen_cache: tuple[float, bool] = (0.0, False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start all subsystems in background threads. Non-blocking."""
        # Make process DPI-aware so monitor coords are in physical pixels
        ctypes.windll.user32.SetProcessDPIAware()
        self._setup_win32_api()

        self._monitors = get_monitors()
        self._edge.update_server_monitors(self._monitors)
        log.info("Server monitors: %s", self._monitors)

        self._capture.start()
        self._clipboard.start()
        self._audio.start()
        self._discovery.start()

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
        self._audio.stop()
        self._discovery.stop()

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
            if monitors and client.hostname in self._placement_memory:
                anchor_id, anchor_edge, offset_px = self._placement_memory[client.hostname]
                placement = VirtualPlacement(
                    client_id=client.client_id,
                    anchor_monitor_id=anchor_id,
                    anchor_edge=anchor_edge,
                    offset_pixels=offset_px,
                )
                self._edge.update_placement(placement, monitors[0])
                client.placement = placement  # carried into on_client_connected for GUI
                log.info("Restored placement for %s (%s edge, anchor %d)", client.hostname, anchor_edge, anchor_id)
            if self.on_client_connected:
                self.on_client_connected(client)  # refresh GUI (placement already set)

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
                # Release client control when a fullscreen game is in the foreground.
                # A fullscreen game warps the cursor and fights our delta tracking —
                # better to hand it back immediately.
                if self._is_fullscreen_foreground():
                    log.debug("Fullscreen window detected — releasing client control")
                    self._release_control()
                    return

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
                    # Warp cursor back to the crossing point, clamped to the full
                    # virtual desktop. Using anchor-only bounds would clip _virt_x/y
                    # to the anchor monitor even when the zone spans multiple monitors
                    # (e.g. a "bottom" zone that straddles M1 and M2).
                    vl, vt, vw, vh = get_virtual_desktop_rect()
                    ret_x = max(vl, min(vl + vw - 1, int(self._virt_x)))
                    ret_y = max(vt, min(vt + vh - 1, int(self._virt_y)))
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
                # Skip edge detection when a fullscreen window has the foreground —
                # prevents cursor warps at game launch from accidentally triggering a grant.
                if self._is_fullscreen_foreground():
                    return

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
            # For bottom/top edges the trigger zone overlaps the server monitor
            # boundary by 1px. Push _virt into the zone so micro-jitter doesn't
            # immediately fire the release condition.
            edge = zone.placement.anchor_edge
            if edge == "bottom":
                self._virt_y = float(zone.rect.top) + 5
            elif edge == "top":
                self._virt_y = float(zone.rect.bottom) - 5
        else:
            self._last_raw_x = hit_x
            self._last_raw_y = hit_y
            
        client = self._client_mgr.get(client_id)
        if client:
            client.send(proto.make_control_grant())
        # Prevent server display/sleep while input is forwarded to client.
        # Without ES_CONTINUOUS the call simply resets the idle timer once;
        # _heartbeat_loop renews it every HEARTBEAT_INTERVAL while forwarding,
        # so no per-thread state to clear when _release_control is called.
        ctypes.windll.kernel32.SetThreadExecutionState(
            0x00000001 | 0x00000002  # ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
        log.info("Control granted to %s", client_id)

        # The cursor is hidden via SetSystemCursor (see show_cursor).
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
    # Win32 API setup
    # ------------------------------------------------------------------

    def _setup_win32_api(self) -> None:
        """Set argtypes/restype for Win32 functions used in hot paths. Called once at start."""
        u32 = ctypes.windll.user32
        u32.GetForegroundWindow.restype    = ctypes.c_void_p
        u32.MonitorFromWindow.restype      = ctypes.c_void_p
        u32.GetClassNameW.argtypes         = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        u32.GetClassNameW.restype          = ctypes.c_int

    # ------------------------------------------------------------------
    # Fullscreen detection
    # ------------------------------------------------------------------

    def _is_fullscreen_foreground(self) -> bool:
        """Return True if the foreground window covers an entire monitor.

        Result is cached for _FULLSCREEN_CACHE_TTL seconds to avoid a Win32
        round-trip on every mouse-move event.
        """
        now = time.monotonic()
        ts, cached = self._fullscreen_cache
        if now - ts < _FULLSCREEN_CACHE_TTL:
            return cached
        result = self._check_fullscreen_win32()
        self._fullscreen_cache = (now, result)
        return result

    # Shell/desktop/taskbar window classes — always cover the full screen by definition,
    # so they must be excluded to avoid false-positive fullscreen detection when the
    # desktop briefly becomes the foreground window (e.g. right after waking from sleep).
    _SHELL_WINDOW_CLASSES = frozenset({
        "Progman",                    # Program Manager / desktop background
        "WorkerW",                    # Desktop wallpaper renderer
        "Shell_TrayWnd",              # Primary taskbar
        "Shell_SecondaryTrayWnd",     # Taskbar on secondary monitors
        "DWMInputSink",               # DWM compositor helper
    })

    def _check_fullscreen_win32(self) -> bool:
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False

            # Skip shell/desktop windows — they always cover the full monitor by
            # definition and would produce a permanent false positive.
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            if class_buf.value in self._SHELL_WINDOW_CLASSES:
                return False

            win_rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(win_rect))

            monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            if not monitor:
                return False

            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            user32.GetMonitorInfoW(monitor, ctypes.byref(mi))

            return (
                win_rect.left  <= mi.rcMonitor.left  and
                win_rect.top   <= mi.rcMonitor.top   and
                win_rect.right >= mi.rcMonitor.right and
                win_rect.bottom >= mi.rcMonitor.bottom
            )
        except Exception:
            return False

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
        client = self._client_mgr.get(placement.client_id)
        if client:
            self._placement_memory[client.hostname] = (
                placement.anchor_monitor_id,
                placement.anchor_edge,
                placement.offset_pixels,
            )
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
            # Keep display + system awake while forwarding (renewed each beat;
            # stops automatically when _active_client_id is cleared).
            if self._active_client_id:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    0x00000001 | 0x00000002  # ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
            now = time.time()
            for client in self._client_mgr.all_clients():
                if now - client.last_pong > HEARTBEAT_TIMEOUT:
                    log.warning("Client %s timed out", client.hostname)
                    self._disconnect_client(client)
                else:
                    client.send(proto.make_ping())
