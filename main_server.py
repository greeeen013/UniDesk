"""
UniDesk — Server entry point.

Usage:
    python main_server.py [--port PORT] [--shutdown SECONDS]

Requires: Windows, PyQt6, pywin32
Admin rights: NOT required for basic use.
  Run as Administrator to also capture input from elevated windows
  (UAC dialogs, Task Manager, etc.).
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

def main():
    parser = argparse.ArgumentParser(description="UniDesk server (main station)")
    parser.add_argument("--port", type=int, default=25432, help="TCP port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    parser.add_argument("--shutdown", type=int, default=0, help="Automatically shutdown after SECONDS (for debugging)")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if args.shutdown > 0:
        logging.info(f"Scheduled auto-shutdown in {args.shutdown} seconds.")
        QTimer.singleShot(args.shutdown * 1000, app.quit)

    from unidesk.server.server_app import ServerApp
    from unidesk.gui.main_window import MainWindow

    server = ServerApp(port=args.port)
    server.start()

    window = MainWindow(server_app=server)
    window.show()

    exit_code = app.exec()
    server.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
