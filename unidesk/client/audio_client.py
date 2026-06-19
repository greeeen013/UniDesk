"""
Audio client — captures PC2 system audio output via WASAPI loopback
and streams it over a dedicated TCP connection to the server (PC1).

No admin rights needed: WASAPI loopback is a standard Windows API feature
that any user-level process can open.  No virtual audio driver required.

Requires: pip install PyAudioWPatch
(PyAudioWPatch is a drop-in pyaudio replacement that exposes WASAPI loopback)

UX note: loopback captures what the default output device *renders*, which
means PC2's speakers will still play audio unless you set PC2's default
output to a device with no physical speakers (e.g. "Digital Output (S/PDIF)"
or a dummy USB dongle).  Muting/lowering PC2 volume does NOT silence the
loopback — Windows applies volume after capture in shared mode.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from typing import Optional

from ..common.constants import AUDIO_CHUNK, AUDIO_PORT_OFFSET

log = logging.getLogger(__name__)

try:
    import pyaudiowpatch as pyaudio
    _AVAILABLE = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _AVAILABLE = False


class AudioClient:
    """Captures system audio output on PC2 and streams PCM to PC1."""

    def __init__(self, host: str, port: int, client_id: str) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_control_port(cls, host: str, control_port: int, client_id: str) -> "AudioClient":
        return cls(host=host, port=control_port + AUDIO_PORT_OFFSET, client_id=client_id)

    def start(self) -> None:
        if not _AVAILABLE:
            log.warning(
                "PyAudioWPatch not installed — audio streaming disabled. "
                "Run: pip install PyAudioWPatch"
            )
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="audio-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            try:
                self._session()
            except Exception as exc:
                if self._running:
                    log.warning("Audio session error: %s — retrying in 3 s", exc)
                    time.sleep(3)

    def _session(self) -> None:
        p = pyaudio.PyAudio()
        stream = None
        sock = None
        try:
            device = self._find_loopback_device(p)
            if device is None:
                log.warning("No WASAPI loopback device found — retrying in 5 s")
                time.sleep(5)
                return

            sample_rate = int(device["defaultSampleRate"])
            channels = min(int(device["maxInputChannels"]), 2)

            log.info(
                "Audio loopback: device='%s'  rate=%d  ch=%d",
                device["name"], sample_rate, channels,
            )

            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=AUDIO_CHUNK,
            )

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((self._host, self._port))

            header = json.dumps({
                "client_id": self._client_id,
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_width": 2,
            }).encode()
            sock.sendall(struct.pack(">I", len(header)) + header)

            log.info("Audio stream connected to %s:%d", self._host, self._port)

            while self._running:
                data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                if data:
                    sock.sendall(struct.pack(">I", len(data)) + data)

        except (OSError, BrokenPipeError) as exc:
            if self._running:
                log.debug("Audio socket closed: %s", exc)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    @staticmethod
    def _find_loopback_device(p: "pyaudio.PyAudio") -> Optional[dict]:
        """Return the WASAPI loopback device for the current default output."""
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return None

        default_out = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        default_name: str = default_out["name"]

        for loopback in p.get_loopback_device_info_generator():
            if default_name in loopback["name"]:
                return loopback

        # Fallback: return first available loopback
        for loopback in p.get_loopback_device_info_generator():
            return loopback

        return None
