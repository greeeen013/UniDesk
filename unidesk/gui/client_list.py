"""Widget showing connected clients with status info."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QScrollArea, QMenu,
)
from PyQt6.QtCore import Qt


class ClientRow(QFrame):
    def __init__(
        self,
        client_id: str,
        hostname: str,
        on_disconnect: Callable[[str], None],
        get_monitor_count: Optional[Callable[[str], int]] = None,
        on_view_screen: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        super().__init__()
        self.client_id = client_id
        self._get_monitor_count = get_monitor_count
        self._on_view_screen = on_view_screen
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self._name_label = QLabel(f"<b>{hostname}</b>")
        self._status_label = QLabel("connected")
        self._status_label.setStyleSheet("color: #4caf50;")
        btn = QPushButton("Disconnect")
        btn.setFixedWidth(90)
        btn.clicked.connect(lambda: on_disconnect(client_id))

        layout.addWidget(self._name_label)
        layout.addWidget(self._status_label)
        layout.addStretch()
        layout.addWidget(btn)

    def set_status(self, text: str, color: str = "#4caf50") -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(f"color: {color};")

    def _show_context_menu(self, pos) -> None:
        if not self._on_view_screen:
            return
        count = self._get_monitor_count(self.client_id) if self._get_monitor_count else 0
        if count <= 0:
            return
        menu = QMenu(self)
        for i in range(count):
            action = menu.addAction(f"View Monitor {i + 1}")
            action.triggered.connect(lambda checked=False, idx=i: self._on_view_screen(self.client_id, idx))
        menu.exec(self.mapToGlobal(pos))


class ClientListWidget(QWidget):
    def __init__(
        self,
        on_disconnect: Callable[[str], None],
        get_monitor_count: Optional[Callable[[str], int]] = None,
        on_view_screen: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        super().__init__()
        self._on_disconnect = on_disconnect
        self._get_monitor_count = get_monitor_count
        self._on_view_screen = on_view_screen
        self._rows: dict[str, ClientRow] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._list_layout = QVBoxLayout(inner)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(inner)

        self._empty_label = QLabel("No clients connected")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: gray; margin: 20px;")
        self._list_layout.addWidget(self._empty_label)

        outer.addWidget(scroll)

    def add_client(self, client_id: str, hostname: str) -> None:
        if client_id in self._rows:
            return
        self._empty_label.hide()
        row = ClientRow(
            client_id, hostname, self._on_disconnect,
            get_monitor_count=self._get_monitor_count,
            on_view_screen=self._on_view_screen,
        )
        self._rows[client_id] = row
        self._list_layout.addWidget(row)

    def remove_client(self, client_id: str) -> None:
        row = self._rows.pop(client_id, None)
        if row:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        if not self._rows:
            self._empty_label.show()
