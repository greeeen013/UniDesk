"""Shared constants for UniDesk."""

APP_NAME = "UniDesk"
APP_VERSION = "0.1.0"

# Network
TCP_PORT = 25432
UDP_DISCOVERY_PORT = 25433  # UDP — no conflict with TCP audio port
AUDIO_PORT_OFFSET = 2       # audio TCP = main_port + 2 (= 25434 by default)
HEARTBEAT_INTERVAL = 5.0   # seconds between PINGs
HEARTBEAT_TIMEOUT = 10.0   # drop client if no PONG within this time

# Audio streaming (client → server)
AUDIO_CHUNK = 1024          # frames per buffer (~21 ms at 48 kHz)
AUDIO_JITTER_BUFFER = 8     # max queued chunks before dropping (~170 ms headroom)

# Screen view streaming (client → server), on-demand per view window
SCREEN_PORT_OFFSET = 3      # screen TCP = main_port + 3 (= 25435 by default)
SCREEN_CAPTURE_FPS = 12
SCREEN_JPEG_QUALITY = 55

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
    AUDIO_ENABLE = "AUDIO_ENABLE"
    AUDIO_DISABLE = "AUDIO_DISABLE"
    SCREEN_VIEW_START = "SCREEN_VIEW_START"
    SCREEN_VIEW_STOP = "SCREEN_VIEW_STOP"
    POWER_ACTION = "POWER_ACTION"

# Edge detector
EDGE_SNAP_TOLERANCE = 10   # pixels — snap distance in GUI
LOCAL_MOUSE_GRAB_THRESHOLD = 30  # physical mouse delta to trigger local grab on client

# GUI scale: 1 logical unit = N screen pixels
GUI_SCALE = 10
