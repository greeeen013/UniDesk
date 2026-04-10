"""Monitor enumeration for the client (same logic as server side)."""

from __future__ import annotations

# Re-use the same implementation
from ..server.monitor_info import get_monitors, get_virtual_desktop_rect

__all__ = ["get_monitors", "get_virtual_desktop_rect"]
