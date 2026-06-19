"""
Audio server — receives PCM stream from AudioClient (PC2) and plays it
through the server's default output device (headphones on PC1).

Runs on a dedicated TCP port (control_port + 2) separate from the JSON
control channel to avoid serialisation overhead on audio data.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import struct
import threading
from typing import Optional

from ..common.constants import AUDIO_CHUNK, AUDIO_JITTER_BUFFER, AUDIO_PORT_OFFSET

log = logging.getLogger(__name__)

try:
    import pyaudiowpatch as pyaudio
    _AVAILABLE = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _AVAILABLE = False


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class AudioServer:
    """Listens for incoming audio streams from clients and plays them."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_control_port(cls, control_port: int) -> "AudioServer":
        return cls(port=control_port + AUDIO_PORT_OFFSET)

    def start(self) -> None:
        if not _AVAILABLE:
            log.warning(
                "PyAudioWPatch not installed — audio streaming disabled. "
                "Run: pip install PyAudioWPatch"
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen, name="audio-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    def _listen(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", self._port))
        except OSError as exc:
            log.error("Cannot bind audio port %d: %s", self._port, exc)
            return
        srv.listen(4)
        srv.settimeout(1.0)
        log.info("Audio server listening on port %d", self._port)

        while self._running:
            try:
                conn, addr = srv.accept()
                log.info("Audio connection from %s", addr[0])
                t = threading.Thread(
                    target=self._handle_stream,
                    args=(conn, addr),
                    name=f"audio-recv-{addr[0]}",
                    daemon=True,
                )
                t.start()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    log.warning("Audio accept error: %s", exc)

        srv.close()

    # ------------------------------------------------------------------
    # Per-connection handler
    # ------------------------------------------------------------------

    def _handle_stream(self, conn: socket.socket, addr: tuple) -> None:
        p = None
        out_stream = None
        buf: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_JITTER_BUFFER)

        try:
            # Read JSON header
            raw_len = _recv_exact(conn, 4)
            if not raw_len:
                return
            header_len = struct.unpack(">I", raw_len)[0]
            if header_len > 4096:
                log.warning("Audio header too large from %s", addr[0])
                return
            header_data = _recv_exact(conn, header_len)
            if not header_data:
                return
            header = json.loads(header_data.decode())

            sample_rate: int = header["sample_rate"]
            channels: int = header["channels"]
            client_id: str = header.get("client_id", "?")
            log.info(
                "Audio stream client=%s  rate=%d  ch=%d",
                client_id, sample_rate, channels,
            )

            # Open output on server default device
            p = pyaudio.PyAudio()
            out_stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                output=True,
                frames_per_buffer=AUDIO_CHUNK,
            )

            playback = threading.Thread(
                target=self._playback_loop,
                args=(out_stream, buf),
                name=f"audio-play-{addr[0]}",
                daemon=True,
            )
            playback.start()

            while self._running:
                raw_len = _recv_exact(conn, 4)
                if not raw_len:
                    break
                chunk_len = struct.unpack(">I", raw_len)[0]
                if chunk_len > 65_536:
                    log.warning("Oversized audio chunk (%d B) — dropping connection", chunk_len)
                    break
                chunk = _recv_exact(conn, chunk_len)
                if not chunk:
                    break

                # Latency control: drop the oldest chunk when buffer is full
                # so we never accumulate > AUDIO_JITTER_BUFFER * ~21 ms of lag.
                if buf.full():
                    try:
                        buf.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    buf.put_nowait(chunk)
                except queue.Full:
                    pass

        except Exception as exc:
            log.warning("Audio stream error from %s: %s", addr[0], exc)
        finally:
            buf.put(b"")  # sentinel — stop playback thread
            if out_stream is not None:
                try:
                    out_stream.stop_stream()
                    out_stream.close()
                except Exception:
                    pass
            if p is not None:
                p.terminate()
            try:
                conn.close()
            except Exception:
                pass
            log.info("Audio stream from %s ended", addr[0])

    @staticmethod
    def _playback_loop(stream: "pyaudio.Stream", buf: "queue.Queue[bytes]") -> None:
        while True:
            try:
                chunk = buf.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk == b"":
                break
            try:
                stream.write(chunk)
            except Exception as exc:
                log.warning("Playback write error: %s", exc)
                break
