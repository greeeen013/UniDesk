"""
Screen view window — shows a live JPEG stream from a client's monitor.

The window is resizable, but each resize is snapped back to the source
monitor's aspect ratio (whichever dimension the user is actively dragging
wins), so the video always fills the window exactly — no black bars.
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
        self._aspect = monitor_width / monitor_height if monitor_height else 16 / 9
        self._enforcing_resize = False

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

        init_w = min(monitor_width, _INIT_MAX_WIDTH) or _INIT_MAX_WIDTH
        init_h = max(1, round(init_w / self._aspect))
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
    # Aspect-ratio-locked resizing (no letterboxing)
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._enforcing_resize:
            self._enforce_aspect(event.size(), event.oldSize())
        self._rescale()

    def _enforce_aspect(self, new_size, old_size) -> None:
        if old_size.width() <= 0 or old_size.height() <= 0:
            return  # first resize (window creation) — already sized correctly
        # Whichever dimension changed the most is the one the user is dragging;
        # derive the other dimension from it so the window snaps back onto
        # the source aspect ratio instead of drifting into letterbox territory.
        dw = abs(new_size.width() - old_size.width())
        dh = abs(new_size.height() - old_size.height())
        if dw >= dh:
            target_w = new_size.width()
            target_h = max(1, round(target_w / self._aspect))
        else:
            target_h = new_size.height()
            target_w = max(1, round(target_h * self._aspect))
        if target_w != new_size.width() or target_h != new_size.height():
            self._enforcing_resize = True
            self.resize(target_w, target_h)
            self._enforcing_resize = False

    def _rescale(self) -> None:
        if self._orig_pixmap is None:
            return
        # Window is kept locked to the source aspect ratio, so filling the
        # label exactly (no KeepAspectRatio) never visibly distorts the image
        # and avoids the 1px rounding slivers KeepAspectRatio would leave.
        scaled = self._orig_pixmap.scaled(
            self._label.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)

    def closeEvent(self, event) -> None:
        self._on_close()
        super().closeEvent(event)
