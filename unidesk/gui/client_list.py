"""Widget showing connected clients with status info."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QScrollArea,
)
from PyQt6.QtCore import Qt


class ClientRow(QFrame):
    def __init__(self, client_id: str, hostname: str, on_disconnect: Callable[[str], None]) -> None:
        super().__init__()
        self.client_id = client_id
        self.setFrameShape(QFrame.Shape.StyledPanel)
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


class ClientListWidget(QWidget):
    def __init__(self, on_disconnect: Callable[[str], None]) -> None:
        super().__init__()
        self._on_disconnect = on_disconnect
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
        row = ClientRow(client_id, hostname, self._on_disconnect)
        self._rows[client_id] = row
        self._list_layout.addWidget(row)

    def remove_client(self, client_id: str) -> None:
        row = self._rows.pop(client_id, None)
        if row:
            self._list_layout.removeWidget(row)
            row.deleteLater()
        if not self._rows:
            self._empty_label.show()
