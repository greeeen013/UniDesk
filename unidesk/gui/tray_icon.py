"""System tray icon for UniDesk server."""

from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QColor


def _make_icon(color: str = "#4caf50") -> QIcon:
    """Generate a simple 16x16 colored square icon."""
    pix = QPixmap(16, 16)
    pix.fill(QColor(color))
    return QIcon(pix)


class TrayIcon(QSystemTrayIcon):
    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        super().__init__(_make_icon())
        self.setToolTip("UniDesk")

        menu = QMenu()
        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(on_show)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(on_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self._on_show = on_show

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_show()

    def set_active(self, active: bool) -> None:
        color = "#4caf50" if active else "#f44336"
        self.setIcon(_make_icon(color))
