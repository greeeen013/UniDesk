"""
Screen view window — shows a live JPEG stream from a client's monitor.

The window is freely resizable; the image is letterboxed (scaled with
Qt.AspectRatioMode.KeepAspectRatio) so it never stretches out of proportion.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

_INIT_MAX_WIDTH = 960


class ScreenViewWindow(QDialog):
    frame_ready = pyqtSignal(bytes, int, int)   # jpeg_bytes, width, height

    def __init__(
        self,
        hostname: str,
        monitor_label: str,
        monitor_width: int,
        monitor_height: int,
        on_close: Callable[[], None],
    ) -> None:
        super().__init__()
        self._on_close = on_close
        self._orig_pixmap: Optional[QPixmap] = None

        self.setWindowTitle(f"{hostname} — {monitor_label}")
        self.setSizeGripEnabled(True)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("background-color: black;")
        self._label.setMinimumSize(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

        self.frame_ready.connect(self._on_frame)

        aspect = monitor_width / monitor_height if monitor_height else 16 / 9
        init_w = min(monitor_width, _INIT_MAX_WIDTH) or _INIT_MAX_WIDTH
        init_h = max(1, int(init_w / aspect))
        self.resize(init_w, init_h)

    # ------------------------------------------------------------------
    # Frame delivery (safe to call from any thread)
    # ------------------------------------------------------------------

    def push_frame_threadsafe(self, data: bytes, width: int, height: int) -> None:
        self.frame_ready.emit(data, width, height)

    def _on_frame(self, data: bytes, width: int, height: int) -> None:
        pixmap = QPixmap()
        if pixmap.loadFromData(data, "JPEG"):
            self._orig_pixmap = pixmap
            self._rescale()

    # ------------------------------------------------------------------
    # Aspect-ratio-preserving scaling
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._orig_pixmap is None:
            return
        scaled = self._orig_pixmap.scaled(
            self._label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)

    def closeEvent(self, event) -> None:
        self._on_close()
        super().closeEvent(event)
