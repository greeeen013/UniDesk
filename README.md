# UniDesk

A software KVM switch over a local network — share your keyboard and mouse from one PC to others simply by moving the cursor across a monitor boundary.

Works on Windows, no additional hardware required. Inspired by tools like Barrier / Synergy, built from scratch in Python.

---

## What it does

- **Main station (PC1)** has the physical keyboard and mouse and runs the server.
- **Other stations (PC2, PC3, …)** run the client and connect over the network.
- In the GUI on PC1 you drag PC2's virtual monitor next to your physical monitors.
- Once you move the mouse across that boundary, the cursor disappears on PC1 and you control PC2 — keyboard and mouse both follow.
- Move the mouse back and you control PC1 again.
- The clipboard (Ctrl+C) is automatically synced between both PCs.

### Input switching behaviour

| Situation | What happens |
|---|---|
| Mouse on PC1 | Keyboard + mouse control PC1 |
| Mouse crosses into virtual monitor | Cursor hides on PC1, you control PC2 |
| PC2 user physically moves their mouse | PC2 takes local control |
| Mouse returns to PC1 | PC1 regains control |

---

## Requirements

- **Windows 10 / 11** on both PCs
- **Python 3.10+**
- Both PCs on the same local network
- Administrator rights **not required** for normal use

> **Note on admin rights:** Low-level hooks (`WH_MOUSE_LL`, `WH_KEYBOARD_LL`) and `SendInput` work without elevation for regular windows. To also capture or inject input into UAC dialogs and Task Manager, run the server as Administrator. The client can be elevated automatically using the `--admin` flag if needed.

---

## Installation

```bash
pip install PyQt6 pywin32
```

---

## Usage

### 1. Find PC1's IP address

Run in a command prompt on PC1:
```
ipconfig
```
Look for `IPv4 Address` — e.g. `192.168.1.10`.

### 2. Start the server on PC1 (main station)

```bash
python main_server.py
```

A GUI window opens showing your monitor layout. The server listens on port `25432`.

### 3. Start the client on PC2

```bash
python main_client.py --server 192.168.1.10
```

Replace `192.168.1.10` with PC1's IP address. A tray icon appears on PC2 — green means connected.

### Command Line Arguments

#### Server (`main_server.py`)
| Argument | Type | Default | Description |
|---|---|---|---|
| `--port` | `int` | `25432` | TCP port to listen on. |
| `--sensitivity` | `float` | `1.0` | Mouse sensitivity multiplier. Syncs physical DPI gaps when controlling a client. |
| `--scale-to-snap` | flag | `off` | Scale virtual trigger zones to match the physical monitor's edge exactly. |
| `--hide-mouse` | flag | `off` | Teleport the server's mouse to the bottom-right corner when it's controlled a client. |
| `--debug` | flag | `off` | Enable detailed debug logging. |
| `--shutdown` | `int` | `0` | Automatically shutdown the server after N seconds (useful for debugging). |

#### Client (`main_client.py`)
| Argument | Type | Default | Description |
|---|---|---|---|
| `--server` | `str` | **Required** | IP address or hostname of the UniDesk server. |
| `--port` | `int` | `25432` | TCP port of the server. |
| `--admin` | flag | `off` | Request Administrator privileges to bypass UIPI (allows clicking on Taskbar/Start). |
| `--hide-mouse` | flag | `off` | Teleport the client's mouse to the bottom-right corner when the server releases control. |

---

### 4. Set up the monitor layout

In the GUI on PC1 (tab **Monitor Layout**):
- Gray blocks = PC1's physical monitors
- Colored block = PC2's virtual monitor

Drag the colored block to the edge of one of the gray monitors — left, right, top, or bottom. It will snap into place.

From that point on, moving the mouse past that edge switches control to PC2.

---

## Project structure

```
UniDesk/
├── main_server.py              # Run on PC1
├── main_client.py              # Run on PC2
├── requirements.txt
└── unidesk/
    ├── common/
    │   ├── protocol.py         # Network protocol (length-prefixed JSON)
    │   ├── config.py           # Shared dataclasses (MonitorRect, VirtualPlacement)
    │   └── constants.py        # Ports, timeouts, constants
    ├── server/
    │   ├── server_app.py       # Server orchestration
    │   ├── input_capture.py    # Win32 low-level hooks — captures keyboard and mouse
    │   ├── monitor_info.py     # Physical monitor detection (EnumDisplayMonitors)
    │   ├── edge_detector.py    # Cursor boundary logic and coordinate translation
    │   ├── client_manager.py   # Connected client management
    │   └── clipboard_server.py # Clipboard monitoring (WM_CLIPBOARDUPDATE)
    ├── client/
    │   ├── client_app.py       # Client orchestration
    │   ├── input_simulator.py  # Win32 SendInput — mouse and keyboard simulation
    │   ├── cursor_manager.py   # Cursor hide/show, physical mouse grab detection
    │   ├── clipboard_client.py # Clipboard sync on the client side
    │   └── monitor_info_client.py
    └── gui/
        ├── main_window.py      # Main window (PyQt6), 3 tabs
        ├── monitor_layout.py   # Drag-and-drop monitor layout (QGraphicsScene)
        ├── client_list.py      # Connected clients list
        └── tray_icon.py        # System tray icon
```

---

## How it works

### Network protocol

TCP connection on port `25432`. Each message is length-prefixed JSON:

```
[4 bytes: message length (big-endian uint32)][N bytes: UTF-8 JSON]
```

Message types: `HANDSHAKE_REQ/ACK`, `MONITOR_INFO`, `MOUSE_MOVE`, `MOUSE_BUTTON`, `MOUSE_SCROLL`, `KEY_EVENT`, `CLIPBOARD_PUSH`, `CONTROL_GRANT`, `CONTROL_RELEASE`, `PING/PONG`.

### Input capture (server)

The server installs Win32 **low-level hooks** via `SetWindowsHookEx`:
- `WH_MOUSE_LL` — captures mouse movement, clicks, scroll
- `WH_KEYBOARD_LL` — captures every key press and release

The hooks run on a dedicated thread with a Win32 message pump. The callback only enqueues events and returns immediately (must be fast — Windows auto-removes hooks whose callback exceeds ~200 ms). When forwarding is active, the hook returns `1` instead of calling `CallNextHookEx`, which suppresses the event from reaching the local system.

### Edge detection

The user positions PC2's monitor in the GUI by dragging it to the edge of a PC1 monitor. This defines a **virtual rectangle** in PC1's unified coordinate space. On every `WM_MOUSEMOVE` the detector checks whether the cursor has entered that rectangle:

- **Entry** → send `CONTROL_GRANT`, lock cursor at the boundary pixel (`SetCursorPos`), hide it (`ShowCursor(False)`), translate coordinates to the client's coordinate space and send over TCP.
- **Exit** → send `CONTROL_RELEASE`, restore cursor.

Coordinate translation:
```python
client_x = int((cx - virt_rect.left) / virt_rect.width  * client_monitor.width)
client_y = int((cy - virt_rect.top)  / virt_rect.height * client_monitor.height)
```

### Input simulation (client)

The client receives messages and simulates input via Win32 `SendInput` — a low-level API that injects events as if they came from hardware. Mouse absolute positioning is normalised to `[0, 65535]` across the full virtual desktop (`MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`). Keyboard events are injected via VK code for maximum compatibility with system shortcuts (Alt+Tab, Win+D, Alt+F4, …).

### Physical grab on PC2

The client installs a local `WH_MOUSE_LL` hook that suppresses physical mouse input while the server is controlling it. If the physical movement delta exceeds a threshold (30 px), it signals that the user at PC2 moved their own mouse — the client sends `CONTROL_RELEASE_REQUEST` and the server returns control.

### Clipboard sync

Both sides register a Win32 `AddClipboardFormatListener` on a hidden message-only window. On `WM_CLIPBOARDUPDATE`, the text is read and sent to the other side as `CLIPBOARD_PUSH`. A `_suppress_next` flag prevents the write from echoing back as a second notification.

---

## Limitations

- Windows only (Win32 API)
- Text clipboard only (images are not synced)
- Hooks cannot capture input in elevated windows without running as Administrator
- Ctrl+Alt+Del cannot be forwarded (blocked by Windows by design)

---

## Firewall

PC1 needs TCP port `25432` open for inbound connections. Windows Firewall will likely prompt automatically on first run.

To open it manually:
```
netsh advfirewall firewall add rule name="UniDesk" dir=in action=allow protocol=TCP localport=25432
```
