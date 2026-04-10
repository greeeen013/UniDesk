"""Shared dataclasses used across server and client."""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class MonitorRect:
    """A single monitor's position and size in virtual desktop coordinates."""
    id: int
    left: int
    top: int
    right: int
    bottom: int
    is_primary: bool = False
    name: str = ""

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "is_primary": self.is_primary,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> MonitorRect:
        return cls(
            id=d["id"],
            left=d["left"],
            top=d["top"],
            right=d["right"],
            bottom=d["bottom"],
            is_primary=d.get("is_primary", False),
            name=d.get("name", ""),
        )


@dataclass
class VirtualPlacement:
    """Describes where a client's monitor is placed relative to a server monitor."""
    client_id: str
    anchor_monitor_id: int   # index in server's monitor list
    anchor_edge: str         # "left" | "right" | "top" | "bottom"
    offset_pixels: int = 0  # along the anchor edge

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "anchor_monitor_id": self.anchor_monitor_id,
            "anchor_edge": self.anchor_edge,
            "offset_pixels": self.offset_pixels,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VirtualPlacement:
        return cls(
            client_id=d["client_id"],
            anchor_monitor_id=d["anchor_monitor_id"],
            anchor_edge=d["anchor_edge"],
            offset_pixels=d.get("offset_pixels", 0),
        )
