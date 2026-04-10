"""
Edge detector — translates server monitor layout + virtual placements
into trigger zones and computes client-space coordinates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..common.config import MonitorRect, VirtualPlacement

log = logging.getLogger(__name__)


@dataclass
class VirtualZone:
    """A computed rectangle in unified desktop space that maps to a client monitor."""
    client_id: str
    rect: MonitorRect          # position in unified (server virtual desktop) coords
    client_monitor: MonitorRect  # the actual client monitor size
    placement: VirtualPlacement


def compute_virtual_rect(
    placement: VirtualPlacement,
    server_monitors: list[MonitorRect],
    client_monitor: MonitorRect,
    scale_to_snap: bool = False,
) -> MonitorRect:
    """Compute where the virtual monitor sits in the server's unified coordinate space."""
    anchor = server_monitors[placement.anchor_monitor_id]
    edge = placement.anchor_edge
    off = placement.offset_pixels

    if scale_to_snap:
        # Scale the trigger zone to match the anchor monitor's dimension on the shared edge.
        if edge in ("top", "bottom"):
            left = anchor.left
            right = anchor.right
            if edge == "bottom":
                top = anchor.bottom
                bottom = top + client_monitor.height
            else: # top
                bottom = anchor.top
                top = bottom - client_monitor.height
        else: # left, right
            top = anchor.top
            bottom = anchor.bottom
            if edge == "right":
                left = anchor.right
                right = left + client_monitor.width
            else: # left
                right = anchor.left
                left = right - client_monitor.width
    else:
        # Original logic: trigger zone matches client monitor resolution.
        if edge == "right":
            left = anchor.right
            top = anchor.top + off
            right = left + client_monitor.width
            bottom = top + client_monitor.height
        elif edge == "left":
            right = anchor.left
            left = right - client_monitor.width
            top = anchor.top + off
            bottom = top + client_monitor.height
        elif edge == "bottom":
            top = anchor.bottom
            bottom = top + client_monitor.height
            left = anchor.left + off
            right = left + client_monitor.width
        elif edge == "top":
            bottom = anchor.top
            top = bottom - client_monitor.height
            left = anchor.left + off
            right = left + client_monitor.width
        else:
            raise ValueError(f"Unknown anchor_edge: {edge!r}")

    return MonitorRect(
        id=client_monitor.id,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        name=f"virtual:{placement.client_id}",
    )


class EdgeDetector:
    """
    Maintains the list of virtual zones and decides — for each cursor position —
    which client (if any) should receive control.
    """

    def __init__(self, server_monitors: list[MonitorRect], scale_to_snap: bool = False) -> None:
        self._server_monitors = server_monitors
        self.scale_to_snap = scale_to_snap
        self._zones: list[VirtualZone] = []

    def update_server_monitors(self, monitors: list[MonitorRect]) -> None:
        self._server_monitors = monitors
        # Zones will be recomputed on next update_placement call
        self._rebuild_zones()

    def update_placement(
        self,
        placement: VirtualPlacement,
        client_monitor: MonitorRect,
    ) -> None:
        """Add or update the virtual zone for a client."""
        rect = compute_virtual_rect(placement, self._server_monitors, client_monitor, scale_to_snap=self.scale_to_snap)
        zone = VirtualZone(
            client_id=placement.client_id,
            rect=rect,
            client_monitor=client_monitor,
            placement=placement,
        )
        # Replace existing zone for this client
        self._zones = [z for z in self._zones if z.client_id != placement.client_id]
        self._zones.append(zone)
        log.debug(
            "Virtual zone set for %s: server-space rect=(%d,%d)-(%d,%d), edge=%s",
            placement.client_id, rect.left, rect.top, rect.right, rect.bottom,
            placement.anchor_edge,
        )

    def remove_client(self, client_id: str) -> None:
        self._zones = [z for z in self._zones if z.client_id != client_id]

    def hit_test(self, x: int, y: int) -> Optional[tuple[str, int, int]]:
        """
        Returns (client_id, client_x, client_y) if (x, y) is inside a virtual zone,
        else None.
        """
        for zone in self._zones:
            if zone.rect.contains(x, y):
                client_x, client_y = self._translate(x, y, zone)
                log.debug("hit_test(%d, %d): HIT zone %s → client(%d, %d)", x, y, zone.client_id, client_x, client_y)
                return zone.client_id, client_x, client_y
        return None

    def get_zone(self, client_id: str) -> Optional[VirtualZone]:
        for z in self._zones:
            if z.client_id == client_id:
                return z
        return None

    def get_boundary_point(self, client_id: str) -> Optional[tuple[int, int]]:
        """
        Return the last pixel on the server side just before the virtual zone.
        Used to lock the server cursor at the boundary when control is handed off.
        """
        zone = self.get_zone(client_id)
        if not zone:
            return None
        r = zone.rect
        edge = zone.placement.anchor_edge
        if edge == "right":
            return r.left - 1, (r.top + r.bottom) // 2
        if edge == "left":
            return r.right, (r.top + r.bottom) // 2
        if edge == "bottom":
            return (r.left + r.right) // 2, r.top - 1
        if edge == "top":
            return (r.left + r.right) // 2, r.bottom
        return None

    def _translate(self, x: int, y: int, zone: VirtualZone) -> tuple[int, int]:
        """Translate unified coords to client monitor coords."""
        r = zone.rect
        cm = zone.client_monitor
        rel_x = x - r.left
        rel_y = y - r.top
        client_x = int(rel_x / r.width * cm.width)
        client_y = int(rel_y / r.height * cm.height)
        return client_x, client_y

    def _rebuild_zones(self) -> None:
        rebuilt = []
        for zone in self._zones:
            rect = compute_virtual_rect(
                zone.placement, self._server_monitors, zone.client_monitor,
                scale_to_snap=self.scale_to_snap
            )
            rebuilt.append(VirtualZone(
                client_id=zone.client_id,
                rect=rect,
                client_monitor=zone.client_monitor,
                placement=zone.placement,
            ))
        self._zones = rebuilt
