"""
Audio client — captures PC2 system audio output via WASAPI loopback
and streams it over a dedicated TCP connection to the server (PC1).

No admin rights needed: WASAPI loopback is a standard Windows API feature
that any user-level process can open.  No virtual audio driver required.

Requires: pip install PyAudioWPatch pycaw
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
    _AUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _AUDIO_AVAILABLE = False

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL as _CLSCTX_ALL
    _MUTE_AVAILABLE = True
except ImportError:
    _MUTE_AVAILABLE = False


class _OutputMuter:
    """Saves and restores the mute state of PC2's default audio output."""

    def __init__(self) -> None:
        self._vol = None
        self._was_muted: bool = False

    def mute(self) -> None:
        if not _MUTE_AVAILABLE:
            log.warning("pycaw not installed — local mute skipped. Run: pip install pycaw")
            return
        try:
            dev = AudioUtilities.GetSpeakers()
            iface = dev.Activate(IAudioEndpointVolume._iid_, _CLSCTX_ALL, None)
            self._vol = iface.QueryInterface(IAudioEndpointVolume)
            self._was_muted = bool(self._vol.GetMute())
            if not self._was_muted:
                self._vol.SetMute(1, None)
                log.info("Local audio output muted")
        except Exception as exc:
            log.warning("Could not mute local output: %s", exc)
            self._vol = None

    def restore(self) -> None:
        if self._vol is None:
            return
        try:
            if not self._was_muted:
                self._vol.SetMute(0, None)
                log.info("Local audio output unmuted")
        except Exception as exc:
            log.warning("Could not restore local mute state: %s", exc)
        self._vol = None


class AudioClient:
    """Captures system audio output on PC2 and streams PCM to PC1."""

    def __init__(self, host: str, port: int, client_id: str, mute_local: bool = True) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._mute_local = mute_local
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def from_control_port(
        cls,
        host: str,
        control_port: int,
        client_id: str,
        mute_local: bool = True,
    ) -> "AudioClient":
        return cls(
            host=host,
            port=control_port + AUDIO_PORT_OFFSET,
            client_id=client_id,
            mute_local=mute_local,
        )

    def start(self) -> None:
        if not _AUDIO_AVAILABLE:
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
        muter = _OutputMuter() if self._mute_local else None
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

            if muter:
                muter.mute()

            while self._running:
                data = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                if data:
                    sock.sendall(struct.pack(">I", len(data)) + data)

        except (OSError, BrokenPipeError) as exc:
            if self._running:
                log.debug("Audio socket closed: %s", exc)
        finally:
            if muter:
                muter.restore()
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

        # Fallback: first available loopback
        for loopback in p.get_loopback_device_info_generator():
            return loopback

        return None
