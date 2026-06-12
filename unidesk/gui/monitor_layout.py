"""
Monitor layout widget — shows server monitors and allows drag-and-drop
positioning of client (virtual) monitors.

Server monitors are shown as gray non-movable rectangles.
Client monitors are shown as colored draggable rectangles that snap
to the edges of server monitors.

Scale: GUI_SCALE pixels per real pixel (default 10 → 1920px monitor = 192 units wide).
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QColor, QBrush, QPen, QFont
from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsRectItem, QGraphicsEllipseItem,
    QGraphicsTextItem, QGraphicsItem,
)

from ..common.config import MonitorRect, VirtualPlacement
from ..common.constants import GUI_SCALE, EDGE_SNAP_TOLERANCE

# Colors for clients (cycle through these)
CLIENT_COLORS = [
    QColor(70, 130, 200, 180),
    QColor(70, 180, 100, 180),
    QColor(200, 100, 70, 180),
    QColor(180, 70, 180, 180),
]


class ServerMonitorItem(QGraphicsRectItem):
    """Non-movable server monitor rectangle."""

    def __init__(self, monitor: MonitorRect) -> None:
        self.monitor = monitor
        r = QRectF(
            monitor.left / GUI_SCALE,
            monitor.top / GUI_SCALE,
            monitor.width / GUI_SCALE,
            monitor.height / GUI_SCALE,
        )
        super().__init__(r)
        self.setBrush(QBrush(QColor(80, 80, 80)))
        self.setPen(QPen(QColor(200, 200, 200), 1))
        self.setZValue(0)

        label = QGraphicsTextItem(
            f"{monitor.name}\n{monitor.width}×{monitor.height}", self
        )
        label.setDefaultTextColor(Qt.GlobalColor.white)
        font = QFont()
        font.setPointSize(7)
        label.setFont(font)
        label.setPos(r.x() + 4, r.y() + 4)


class ClientMonitorItem(QGraphicsRectItem):
    """Draggable client monitor rectangle."""

    def __init__(
        self,
        client_id: str,
        hostname: str,
        monitor: MonitorRect,
        color: QColor,
        server_items: list[ServerMonitorItem],
        on_placed: Callable[[VirtualPlacement], None],
        on_snap_preview: Optional[Callable] = None,
        get_snap_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.client_id = client_id
        self.hostname = hostname
        self.client_monitor = monitor
        self._color = color
        self._server_items = server_items
        self._on_placed = on_placed
        self._on_snap_preview = on_snap_preview
        self._get_snap_enabled = get_snap_enabled or (lambda: True)

        r = QRectF(0, 0, monitor.width / GUI_SCALE, monitor.height / GUI_SCALE)
        super().__init__(r)
        self.setBrush(QBrush(color))
        self.setPen(QPen(color.darker(130), 2))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(1)

        label = QGraphicsTextItem(f"{hostname}\n{monitor.width}×{monitor.height}", self)
        label.setDefaultTextColor(Qt.GlobalColor.white)
        font = QFont()
        font.setPointSize(7)
        label.setFont(font)
        label.setPos(4, 4)

    def set_highlight(self, active: Optional[bool]) -> None:
        """active=True: highlighted; active=False: dimmed; active=None: normal."""
        if active is True:
            self.setBrush(QBrush(self._color))
            self.setPen(QPen(QColor(255, 220, 50), 3))
        elif active is False:
            dim = QColor(self._color)
            dim.setAlpha(80)
            self.setBrush(QBrush(dim))
            self.setPen(QPen(self._color.darker(150), 1))
        else:
            self.setBrush(QBrush(self._color))
            self.setPen(QPen(self._color.darker(130), 2))

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._on_snap_preview:
            if self._get_snap_enabled():
                snap = self._find_best_snap()
                self._on_snap_preview((snap[0], snap[1]) if snap else None)
            else:
                self._on_snap_preview(None)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if self._on_snap_preview:
            self._on_snap_preview(None)
        self._snap_and_notify()

    def _find_best_snap(self):
        """
        Return (edge, srv_r, placement, snap_pos) for the nearest snap candidate,
        or None if there are no server monitors.

        Distance metric: 2D Euclidean distance from the client's CENTER to the
        nearest point on each edge segment of each server monitor. This gives
        intuitive results when the client overlaps a corner — the edge whose
        face is geometrically closest wins, rather than whichever axis-aligned
        component happens to be smaller.
        """
        best_dist = float("inf")
        best = None
        my_r = self.sceneBoundingRect()
        cx = my_r.left() + my_r.width() / 2
        cy = my_r.top() + my_r.height() / 2

        for srv_item in self._server_items:
            srv_r = srv_item.sceneBoundingRect()
            mon = srv_item.monitor

            # Nearest point on the vertical (right/left) and horizontal
            # (bottom/top) edge segments to the client center.
            ny = max(srv_r.top(), min(srv_r.bottom(), cy))
            nx = max(srv_r.left(), min(srv_r.right(), cx))

            candidates = [
                # snap_pos: only the perpendicular axis snaps; the parallel axis
                # keeps the client's current position so it doesn't jump to corners.
                ("right",  QPointF(srv_r.right(), my_r.top()),
                 math.hypot(cx - srv_r.right(), cy - ny)),
                ("left",   QPointF(srv_r.left() - my_r.width(), my_r.top()),
                 math.hypot(cx - srv_r.left(), cy - ny)),
                ("bottom", QPointF(my_r.left(), srv_r.bottom()),
                 math.hypot(cx - nx, cy - srv_r.bottom())),
                ("top",    QPointF(my_r.left(), srv_r.top() - my_r.height()),
                 math.hypot(cx - nx, cy - srv_r.top())),
            ]

            for edge, snap_pos, dist in candidates:
                if dist < best_dist:
                    best_dist = dist
                    if edge in ("right", "left"):
                        offset = int((my_r.top() - srv_r.top()) * GUI_SCALE)
                    else:
                        offset = int((my_r.left() - srv_r.left()) * GUI_SCALE)
                    placement = VirtualPlacement(
                        client_id=self.client_id,
                        anchor_monitor_id=mon.id,
                        anchor_edge=edge,
                        offset_pixels=offset,
                    )
                    best = (edge, srv_r, placement, snap_pos)

        return best

    def _snap_and_notify(self) -> None:
        result = self._find_best_snap()
        if result:
            edge, srv_r, placement, snap_pos = result
            if self._get_snap_enabled():
                self.setPos(snap_pos)
            self._on_placed(placement)


class MonitorLayoutWidget(QGraphicsView):
    """
    The main monitor layout view.

    *on_placement_changed* is called whenever a client monitor is dropped.
    Signature: (placement: VirtualPlacement) -> None
    """

    def __init__(self, on_placement_changed: Callable[[VirtualPlacement], None]) -> None:
        self._scene = QGraphicsScene()
        super().__init__(self._scene)
        self._on_placement_changed = on_placement_changed
        self._server_items: list[ServerMonitorItem] = []
        self._client_items: dict[str, ClientMonitorItem] = {}
        self._color_idx = 0
        self._snap_enabled = True

        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

        self._snap_indicator = QGraphicsRectItem()
        self._snap_indicator.setBrush(QBrush(QColor(255, 215, 0, 210)))
        self._snap_indicator.setPen(QPen(QColor(255, 170, 0, 180), 0.5))
        self._snap_indicator.setZValue(5)
        self._snap_indicator.hide()
        self._scene.addItem(self._snap_indicator)

        dot_r = 5
        self._cursor_dot = QGraphicsEllipseItem(-dot_r, -dot_r, dot_r * 2, dot_r * 2)
        self._cursor_dot.setBrush(QBrush(QColor(255, 80, 80)))
        self._cursor_dot.setPen(QPen(QColor(255, 255, 255), 1.5))
        self._cursor_dot.setZValue(10)
        self._cursor_dot.hide()
        self._scene.addItem(self._cursor_dot)

    def set_snap_enabled(self, enabled: bool) -> None:
        self._snap_enabled = enabled
        if not enabled:
            self._snap_indicator.hide()

    def set_server_monitors(self, monitors: list[MonitorRect]) -> None:
        """Rebuild server monitor display."""
        for item in self._server_items:
            self._scene.removeItem(item)
        self._server_items.clear()

        for mon in monitors:
            item = ServerMonitorItem(mon)
            self._scene.addItem(item)
            self._server_items.append(item)

        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))

    def add_client_monitor(
        self,
        client_id: str,
        hostname: str,
        monitor: MonitorRect,
    ) -> None:
        """Add or refresh a client's virtual monitor."""
        if client_id in self._client_items:
            self._scene.removeItem(self._client_items[client_id])

        color = CLIENT_COLORS[self._color_idx % len(CLIENT_COLORS)]
        self._color_idx += 1

        item = ClientMonitorItem(
            client_id=client_id,
            hostname=hostname,
            monitor=monitor,
            color=color,
            server_items=self._server_items,
            on_placed=self._on_placement_changed,
            on_snap_preview=self._update_snap_preview,
            get_snap_enabled=lambda: self._snap_enabled,
        )
        # Default position: to the right of the rightmost server monitor
        if self._server_items:
            rightmost = max(self._server_items, key=lambda i: i.sceneBoundingRect().right())
            r = rightmost.sceneBoundingRect()
            item.setPos(r.right(), r.top())
        self._scene.addItem(item)
        self._client_items[client_id] = item
        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))

    def remove_client_monitor(self, client_id: str) -> None:
        item = self._client_items.pop(client_id, None)
        if item:
            self._scene.removeItem(item)

    def set_active_client(self, client_id: Optional[str]) -> None:
        for cid, item in self._client_items.items():
            if client_id is None:
                item.set_highlight(None)
            else:
                item.set_highlight(cid == client_id)

    def _update_snap_preview(self, snap: Optional[tuple]) -> None:
        """Show a highlight strip on the edge where the dragged client will snap."""
        if snap is None:
            self._snap_indicator.hide()
            return
        edge, srv_r = snap
        THICK = 3.0
        if edge == "right":
            r = QRectF(srv_r.right() - THICK / 2, srv_r.top(), THICK, srv_r.height())
        elif edge == "left":
            r = QRectF(srv_r.left() - THICK / 2, srv_r.top(), THICK, srv_r.height())
        elif edge == "bottom":
            r = QRectF(srv_r.left(), srv_r.bottom() - THICK / 2, srv_r.width(), THICK)
        else:  # top
            r = QRectF(srv_r.left(), srv_r.top() - THICK / 2, srv_r.width(), THICK)
        self._snap_indicator.setRect(r)
        self._snap_indicator.show()

    def restore_client_placement(self, client_id: str, placement: VirtualPlacement) -> None:
        """Reposition a client's monitor block to match a restored VirtualPlacement."""
        item = self._client_items.get(client_id)
        if not item or placement.anchor_monitor_id >= len(self._server_items):
            return
        srv_item = self._server_items[placement.anchor_monitor_id]
        srv_r = srv_item.sceneBoundingRect()
        item_r = item.boundingRect()
        off = placement.offset_pixels / GUI_SCALE
        edge = placement.anchor_edge
        if edge == "right":
            pos = QPointF(srv_r.right(), srv_r.top() + off)
        elif edge == "left":
            pos = QPointF(srv_r.left() - item_r.width(), srv_r.top() + off)
        elif edge == "bottom":
            pos = QPointF(srv_r.left() + off, srv_r.bottom())
        else:  # top
            pos = QPointF(srv_r.left() + off, srv_r.top() - item_r.height())
        item.setPos(pos)

    def update_cursor(self, x: float, y: float) -> None:
        self._cursor_dot.setPos(x / GUI_SCALE, y / GUI_SCALE)
        self._cursor_dot.show()
