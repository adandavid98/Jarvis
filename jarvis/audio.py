"""Windows-friendly bidirectional PCM audio I/O for Jarvis."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import numpy as np
import sounddevice as sd


class AudioInputStream:
    """Captures 16 kHz mono int16 PCM frames and pushes them to an asyncio queue."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        blocksize: int = 1280,
        max_queue_frames: int = 100,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.max_queue_frames = max_queue_frames
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._stream: sd.InputStream | None = None

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if self._loop is None or self._queue is None:
            return
        chunk = indata[:, 0].copy()
        self._loop.call_soon_threadsafe(self._enqueue_nonblocking, chunk)

    def _enqueue_nonblocking(self, chunk: np.ndarray) -> None:
        if self._queue is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    def start(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[np.ndarray]:
        """Start capturing microphone audio and return the frame queue."""
        if self._stream is not None:
            raise RuntimeError("AudioInputStream is already started.")
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self.max_queue_frames)
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()
        return self._queue

    def drain(self) -> None:
        """Discard any buffered input frames."""
        if self._queue is None:
            return
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def stop(self) -> None:
        """Stop and close the microphone stream."""
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None
        self._queue = None
        self._loop = None

    def __enter__(self) -> AudioInputStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class AudioOutputStream:
    """Streams 24 kHz mono int16 PCM chunks to speakers with instant barge-in clear."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: sd.OutputStream | None = None
        self._last_audio_time: float = 0.0

    def _callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        bytes_needed = frames * self.channels * 2  # 2 bytes per int16 sample
        with self._lock:
            available = min(bytes_needed, len(self._buffer))
            if available > 0:
                chunk = bytes(self._buffer[:available])
                del self._buffer[:available]
                self._last_audio_time = time.monotonic()
            else:
                chunk = b""

        if available < bytes_needed:
            chunk = chunk + (b"\x00" * (bytes_needed - available))

        samples = np.frombuffer(chunk, dtype=np.int16).reshape(frames, self.channels)
        outdata[:] = samples

    def start(self) -> None:
        """Open and start the speaker output stream."""
        if self._stream is not None:
            return
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def enqueue(self, pcm_bytes: bytes) -> None:
        """Append 24 kHz int16 PCM bytes for playback."""
        if not pcm_bytes:
            return
        with self._lock:
            self._buffer.extend(pcm_bytes)
            self._last_audio_time = time.monotonic()

    def clear(self) -> None:
        """Immediately discard queued playback audio (used on user interruption)."""
        with self._lock:
            self._buffer.clear()

    @property
    def is_playing(self) -> bool:
        """Return True while audio is queued or played within the last 500ms."""
        with self._lock:
            if len(self._buffer) > 0:
                return True
        return (time.monotonic() - self._last_audio_time) < 0.50

    def stop(self) -> None:
        """Stop and close the speaker output stream."""
        self.clear()
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None

    def __enter__(self) -> AudioOutputStream:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
