"""
Monitor layout widget — shows server monitors and allows drag-and-drop
positioning of client (virtual) monitors.

Server monitors are shown as gray non-movable rectangles.
Client monitors are shown as colored draggable rectangles that snap
to the edges of server monitors.

Scale: GUI_SCALE pixels per real pixel (default 10 → 1920px monitor = 192 units wide).
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QRectF, QPointF
from PyQt6.QtGui import QColor, QBrush, QPen, QFont
from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsRectItem,
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
    ) -> None:
        self.client_id = client_id
        self.hostname = hostname
        self.client_monitor = monitor
        self._server_items = server_items
        self._on_placed = on_placed

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

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._snap_and_notify()

    def _snap_and_notify(self) -> None:
        """Snap to nearest server monitor edge and emit placement."""
        best: Optional[tuple[float, VirtualPlacement, QPointF]] = None

        my_r = self.sceneBoundingRect()

        for srv_item in self._server_items:
            srv_r = srv_item.sceneBoundingRect()
            mon = srv_item.monitor

            candidates = [
                ("right",  QPointF(srv_r.right(), srv_r.top()),
                 abs(my_r.left() - srv_r.right())),
                ("left",   QPointF(srv_r.left() - my_r.width(), srv_r.top()),
                 abs(my_r.right() - srv_r.left())),
                ("bottom", QPointF(srv_r.left(), srv_r.bottom()),
                 abs(my_r.top() - srv_r.bottom())),
                ("top",    QPointF(srv_r.left(), srv_r.top() - my_r.height()),
                 abs(my_r.bottom() - srv_r.top())),
            ]

            for edge, snap_pos, dist in candidates:
                if best is None or dist < best[0]:
                    # Compute pixel offset along the edge
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
                    best = (dist, placement, snap_pos)

        if best:
            _, placement, snap_pos = best
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

        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setBackgroundBrush(QBrush(QColor(30, 30, 30)))

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
