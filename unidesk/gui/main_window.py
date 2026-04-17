"""
Main GUI window for UniDesk server.

Tabs:
  0 — Monitor Layout (drag-and-drop virtual monitors)
  1 — Connected Clients
  2 — Settings
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QCheckBox, QSpinBox, QFormLayout, QStatusBar,
    QApplication,
)

from ..common.config import MonitorRect, VirtualPlacement
from ..common.constants import TCP_PORT
from .client_list import ClientListWidget
from .monitor_layout import MonitorLayoutWidget
from .tray_icon import TrayIcon

log = logging.getLogger(__name__)


class _Signals(QObject):
    """Qt signals for cross-thread GUI updates."""
    client_connected = pyqtSignal(str, str, object)    # client_id, hostname, monitor
    client_disconnected = pyqtSignal(str)              # client_id
    monitors_changed = pyqtSignal(object)              # list[MonitorRect]


class MainWindow(QMainWindow):
    def __init__(self, server_app) -> None:
        super().__init__()
        self._server = server_app
        self._signals = _Signals()
        self._last_active_id: str | None = None
        self._setup_ui()
        self._connect_signals()
        self._setup_tray()
        self._load_server_monitors()
        self._start_cursor_timer()

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle("UniDesk")
        self.setMinimumSize(800, 500)

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        # Tab 0: Monitor layout
        self._layout_widget = MonitorLayoutWidget(
            on_placement_changed=self._on_placement_changed
        )
        tabs.addTab(self._layout_widget, "Monitor Layout")

        # Tab 1: Connected clients
        self._client_list = ClientListWidget(
            on_disconnect=self._on_disconnect_client
        )
        tabs.addTab(self._client_list, "Clients")

        # Tab 2: Settings
        settings_tab = self._build_settings_tab()
        tabs.addTab(settings_tab, "Settings")

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Server starting…")

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        form = QFormLayout()

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(TCP_PORT)
        form.addRow("TCP Port:", self._port_spin)

        self._clipboard_check = QCheckBox("Enable clipboard sync")
        self._clipboard_check.setChecked(True)
        form.addRow("Clipboard:", self._clipboard_check)

        layout.addLayout(form)
        layout.addStretch()
        return w

    def _setup_tray(self) -> None:
        self._tray = TrayIcon(
            on_show=self._toggle_visibility,
            on_quit=QApplication.instance().quit,
        )
        self._tray.show()

    # ------------------------------------------------------------------
    # Signal connections
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._signals.client_connected.connect(self._on_client_connected_gui)
        self._signals.client_disconnected.connect(self._on_client_disconnected_gui)
        self._signals.monitors_changed.connect(self._on_monitors_changed_gui)

        # Wire server callbacks → signals (called from non-GUI threads)
        self._server.on_client_connected = self._emit_client_connected
        self._server.on_client_disconnected = self._emit_client_disconnected
        self._server.on_monitors_changed = self._emit_monitors_changed

    # ------------------------------------------------------------------
    # Thread-safe emitters (called from server threads)
    # ------------------------------------------------------------------

    def _emit_client_connected(self, client) -> None:
        monitor = client.monitors[0] if client.monitors else None
        self._signals.client_connected.emit(client.client_id, client.hostname, monitor)

    def _emit_client_disconnected(self, client) -> None:
        self._signals.client_disconnected.emit(client.client_id)

    def _emit_monitors_changed(self, monitors: list[MonitorRect]) -> None:
        self._signals.monitors_changed.emit(monitors)

    # ------------------------------------------------------------------
    # GUI slots (always on main thread)
    # ------------------------------------------------------------------

    def _on_client_connected_gui(self, client_id: str, hostname: str, monitor) -> None:
        self._client_list.add_client(client_id, hostname)
        if monitor:
            self._layout_widget.add_client_monitor(client_id, hostname, monitor)
        self._status.showMessage(f"{hostname} connected")
        self._tray.set_active(True)

    def _on_client_disconnected_gui(self, client_id: str) -> None:
        self._client_list.remove_client(client_id)
        self._layout_widget.remove_client_monitor(client_id)
        self._status.showMessage("Client disconnected")

    def _on_monitors_changed_gui(self, monitors: list[MonitorRect]) -> None:
        self._layout_widget.set_server_monitors(monitors)

    def _on_placement_changed(self, placement: VirtualPlacement) -> None:
        """Called when user drags a client monitor into a new position."""
        client = self._server._client_mgr.get(placement.client_id)
        if client and client.monitors:
            self._server.set_placement(placement, client.monitors[0])

    def _on_disconnect_client(self, client_id: str) -> None:
        client = self._server._client_mgr.get(client_id)
        if client:
            self._server._disconnect_client(client)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _start_cursor_timer(self) -> None:
        self._cursor_timer = QTimer(self)
        self._cursor_timer.timeout.connect(self._update_cursor_display)
        self._cursor_timer.start(33)  # ~30 Hz

    def _update_cursor_display(self) -> None:
        active_id = self._server._active_client_id
        if active_id != self._last_active_id:
            self._last_active_id = active_id
            self._layout_widget.set_active_client(active_id)

        if active_id:
            x, y = self._server._virt_x, self._server._virt_y
        else:
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            x, y = pt.x, pt.y
        self._layout_widget.update_cursor(x, y)

    def _load_server_monitors(self) -> None:
        monitors = self._server.get_monitors()
        self._layout_widget.set_server_monitors(monitors)
        self._status.showMessage(f"Server ready — {len(monitors)} monitor(s) detected")

    def _toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def closeEvent(self, event) -> None:
        # Minimize to tray instead of quitting
        event.ignore()
        self.hide()
        self._tray.showMessage("UniDesk", "Running in background", TrayIcon.MessageIcon.Information, 2000)
