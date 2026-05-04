from __future__ import annotations

import base64
import json
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import resampy
import sounddevice as sd

from utils import log_event

AUDIO_LEVELS_PATH = Path.home() / ".jarvis" / "audio_levels.json"


def _write_audio_levels(*, mic_level: float | None = None, speaker_level: float | None = None) -> None:
    try:
        AUDIO_LEVELS_PATH.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if AUDIO_LEVELS_PATH.exists():
            try:
                current = json.loads(AUDIO_LEVELS_PATH.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        if mic_level is not None:
            current["mic_level"] = float(mic_level)
        if speaker_level is not None:
            current["speaker_level"] = float(speaker_level)
        current["updated_at"] = time.time()
        AUDIO_LEVELS_PATH.write_text(json.dumps(current), encoding="utf-8")
    except Exception:
        pass


def pcm16_16k_to_base64_24k(audio_16k: np.ndarray) -> str:
    if audio_16k.size == 0:
        return ""
    audio_float = audio_16k.astype(np.float32) / 32768.0
    audio_24k = resampy.resample(audio_float, 16000, 24000)
    audio_int16 = (audio_24k * 32768.0).astype(np.int16)
    return base64.b64encode(audio_int16.tobytes()).decode("utf-8")


class MicRecorder:
    def __init__(self, samplerate: int = 16000, blocksize: int = 1280, device=None):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.device = device
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
        self.stream: sd.InputStream | None = None
        self._last_level_write = 0.0

    def start(self) -> None:
        self.stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="int16",
            blocksize=self.blocksize,
            callback=self._callback,
            device=self.device,
        )
        self.stream.start()
        log_event("mic_started", {"samplerate": self.samplerate, "blocksize": self.blocksize})

    def stop(self) -> None:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        _write_audio_levels(mic_level=0.0)
        log_event("mic_stopped", {})

    def _callback(self, indata, frames, time_info, status):
        mono = np.array(indata[:, 0], dtype=np.int16)
        now = time.time()
        if now - self._last_level_write >= 0.05:
            level = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)) / 32768.0)
            _write_audio_levels(mic_level=min(level * 6.0, 1.0))
            self._last_level_write = now
        try:
            self.q.put_nowait(mono)
        except queue.Full:
            pass

    def read_chunk(self, timeout=0.1):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


class Speaker:
    def __init__(self, samplerate: int = 24000, state_callback: Callable[[str], None] | None = None, blocksize: int = 2048):
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.state_callback = state_callback
        self.is_speaking = False
        self.last_audio_end_at = 0.0
        self._stream: sd.OutputStream | None = None
        self._lock = threading.Lock()

    def _set_state(self, state: str) -> None:
        if self.state_callback:
            try:
                self.state_callback(state)
            except Exception:
                pass

    def _ensure_stream(self) -> None:
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=1,
            dtype="int16",
            blocksize=self.blocksize,
        )
        self._stream.start()

    def start_realtime(self) -> None:
        with self._lock:
            self._ensure_stream()
            if not self.is_speaking:
                self.is_speaking = True
                self._set_state("speaking")

    def play_pcm_chunk(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        with self._lock:
            self._ensure_stream()
            if not self.is_speaking:
                self.is_speaking = True
                self._set_state("speaking")
            audio_np = np.frombuffer(pcm_bytes, dtype=np.int16)
            if audio_np.size > 0:
                level = float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2)) / 32768.0)
                _write_audio_levels(speaker_level=min(level * 4.0, 1.0))
                self._stream.write(audio_np)

    def finish_realtime(self) -> None:
        with self._lock:
            self.is_speaking = False
            self.last_audio_end_at = time.time()
            _write_audio_levels(speaker_level=0.0)
            self._set_state("idle")

    def force_stop(self) -> None:
        """Abort audio playback immediately.

        Calls sd.stop() BEFORE acquiring the lock so it can unblock a
        play_pcm_chunk() call that is currently holding the lock inside a
        blocking stream write.  Safe to call from any thread.
        """
        sd.stop()  # aborts pending stream write without needing the lock
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self.is_speaking = False
            self.last_audio_end_at = time.time()
            _write_audio_levels(speaker_level=0.0)
            self._set_state("idle")

    def interrupt(self) -> None:
        self.force_stop()

    def stop(self):
        with self._lock:
            sd.stop()
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self.is_speaking = False
            self.last_audio_end_at = time.time()
            _write_audio_levels(speaker_level=0.0)
            self._set_state("idle")

    def play_wav_file(self, path: str | Path) -> None:
        wav_path = Path(path).expanduser()
        with wave.open(str(wav_path), "rb") as wf:
            data = wf.readframes(wf.getnframes())
            audio_np = np.frombuffer(data, dtype=np.int16)
        with self._lock:
            self._ensure_stream()
            self.is_speaking = True
            self._set_state("speaking")
            try:
                if audio_np.size > 0:
                    level = float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2)) / 32768.0)
                    _write_audio_levels(speaker_level=min(level * 4.0, 1.0))
                self._stream.write(audio_np)
            finally:
                self.is_speaking = False
                self.last_audio_end_at = time.time()
                _write_audio_levels(speaker_level=0.0)
                self._set_state("idle")
