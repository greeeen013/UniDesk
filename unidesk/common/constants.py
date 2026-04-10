"""Shared constants for UniDesk."""

APP_NAME = "UniDesk"
APP_VERSION = "0.1.0"

# Network
TCP_PORT = 25432
UDP_DISCOVERY_PORT = 25433
HEARTBEAT_INTERVAL = 5.0   # seconds between PINGs
HEARTBEAT_TIMEOUT = 10.0   # drop client if no PONG within this time

# Message types
class MsgType:
    HANDSHAKE_REQ = "HANDSHAKE_REQ"
    HANDSHAKE_ACK = "HANDSHAKE_ACK"
    MONITOR_INFO = "MONITOR_INFO"
    MOUSE_MOVE = "MOUSE_MOVE"
    MOUSE_BUTTON = "MOUSE_BUTTON"
    MOUSE_SCROLL = "MOUSE_SCROLL"
    KEY_EVENT = "KEY_EVENT"
    CLIPBOARD_PUSH = "CLIPBOARD_PUSH"
    CONTROL_GRANT = "CONTROL_GRANT"
    CONTROL_RELEASE = "CONTROL_RELEASE"
    CONTROL_RELEASE_REQUEST = "CONTROL_RELEASE_REQUEST"
    PING = "PING"
    PONG = "PONG"
    ERROR = "ERROR"

# Edge detector
EDGE_SNAP_TOLERANCE = 10   # pixels — snap distance in GUI
LOCAL_MOUSE_GRAB_THRESHOLD = 30  # physical mouse delta to trigger local grab on client

# GUI scale: 1 logical unit = N screen pixels
GUI_SCALE = 10
