"""
UniDesk — Client entry point.

Usage:
    python main_client.py --server SERVER_IP [--port PORT]

Requires: Windows, PyQt6, pywin32
Admin rights: NOT required for basic use.
  Run as Administrator if the server PC runs elevated, to keep input
  injection working across privilege boundaries.
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        logging.info("Requesting Administrator privileges to bypass Windows UIPI (Taskbar interaction)...")
        # Re-run the program with admin rights
        # sys.executable is the python interpreter
        params = " ".join([f'"{arg}"' for arg in sys.argv])
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if ret <= 32:
            logging.error("Failed to elevate gracefully. Try running Command Prompt as Administrator manually.")
        sys.exit(0)

    parser = argparse.ArgumentParser(description="UniDesk client (other station)")
    parser.add_argument("--server", required=True, help="Server IP address or hostname")
    parser.add_argument("--port", type=int, default=25432, help="Server TCP port")
    args = parser.parse_args()

    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
    from PyQt6.QtGui import QPixmap, QColor, QIcon

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from unidesk.client.client_app import ClientApp

    client = ClientApp(server_host=args.server, port=args.port)

    # Minimal tray icon for client
    pix = QPixmap(16, 16)
    pix.fill(QColor("#2196f3"))
    tray = QSystemTrayIcon(QIcon(pix))
    tray.setToolTip(f"UniDesk client → {args.server}")

    from PyQt6.QtWidgets import QMenu
    menu = QMenu()
    quit_action = menu.addAction("Quit")
    quit_action.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.show()

    def on_connected():
        tray.showMessage("UniDesk", f"Connected to {args.server}", QSystemTrayIcon.MessageIcon.Information, 2000)
        pix2 = QPixmap(16, 16)
        pix2.fill(QColor("#4caf50"))
        tray.setIcon(QIcon(pix2))

    def on_disconnected():
        pix3 = QPixmap(16, 16)
        pix3.fill(QColor("#f44336"))
        tray.setIcon(QIcon(pix3))

    client.on_connected = on_connected
    client.on_disconnected = on_disconnected
    client.start()

    exit_code = app.exec()
    client.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
