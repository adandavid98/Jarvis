"""Windows-friendly bidirectional PCM audio I/O for Jarvis with auto-device hot-swap and AGC."""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time
import uuid
from ctypes import wintypes
from typing import Any

import numpy as np
import sounddevice as sd


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text: str) -> _GUID:
        u = uuid.UUID(text)
        d1, d2, d3 = u.fields[:3]
        arr = (ctypes.c_ubyte * 8)(*u.bytes[8:])
        return cls(d1, d2, d3, arr)


_CLSID_MMDeviceEnumerator = _GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = _GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = _GUID.from_str("5CDF2C82-841E-4546-9722-0CF74078229A")


def get_windows_default_endpoints(ensure_mic_unmuted: bool = False) -> tuple[str, str]:
    """Query Windows CoreAudio default Capture and Render endpoint IDs and optionally unmute/boost mic."""
    if sys.platform != "win32":
        return ("", "")
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)
        enum_ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator),
            None,
            23,
            ctypes.byref(_IID_IMMDeviceEnumerator),
            ctypes.byref(enum_ptr),
        )
        if hr != 0 or not enum_ptr.value:
            return ("", "")

        enum_vtbl = ctypes.cast(
            ctypes.cast(enum_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_default = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(enum_vtbl[4])
        enum_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(enum_vtbl[2])

        cap_ids: list[str] = []
        ren_ids: list[str] = []
        try:
            for flow in (0, 1):  # 0 = eRender (speaker), 1 = eCapture (mic)
                for role in (0, 2):  # 0 = eConsole, 2 = eCommunications
                    dev_ptr = ctypes.c_void_p()
                    if get_default(enum_ptr, flow, role, ctypes.byref(dev_ptr)) == 0 and dev_ptr.value:
                        dev_vtbl = ctypes.cast(
                            ctypes.cast(dev_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                            ctypes.POINTER(ctypes.c_void_p),
                        )
                        get_id = ctypes.WINFUNCTYPE(
                            ctypes.c_long,
                            ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_wchar_p),
                        )(dev_vtbl[5])
                        activate = ctypes.WINFUNCTYPE(
                            ctypes.c_long,
                            ctypes.c_void_p,
                            ctypes.POINTER(_GUID),
                            wintypes.DWORD,
                            ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p),
                        )(dev_vtbl[3])
                        dev_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(dev_vtbl[2])

                        str_ptr = ctypes.c_wchar_p()
                        if get_id(dev_ptr, ctypes.byref(str_ptr)) == 0 and str_ptr.value:
                            if flow == 1:
                                cap_ids.append(str_ptr.value)
                            else:
                                ren_ids.append(str_ptr.value)
                            ole32.CoTaskMemFree(str_ptr)

                        if ensure_mic_unmuted and flow == 1:
                            vol_ptr = ctypes.c_void_p()
                            if (
                                activate(
                                    dev_ptr,
                                    ctypes.byref(_IID_IAudioEndpointVolume),
                                    23,
                                    None,
                                    ctypes.byref(vol_ptr),
                                )
                                == 0
                                and vol_ptr.value
                            ):
                                vol_vtbl = ctypes.cast(
                                    ctypes.cast(vol_ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                                    ctypes.POINTER(ctypes.c_void_p),
                                )
                                set_scalar = ctypes.WINFUNCTYPE(
                                    ctypes.c_long, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p
                                )(vol_vtbl[7])
                                get_scalar = ctypes.WINFUNCTYPE(
                                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)
                                )(vol_vtbl[9])
                                set_mute = ctypes.WINFUNCTYPE(
                                    ctypes.c_long, ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p
                                )(vol_vtbl[14])
                                get_mute = ctypes.WINFUNCTYPE(
                                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)
                                )(vol_vtbl[15])
                                vol_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                                    vol_vtbl[2]
                                )

                                muted = wintypes.BOOL()
                                if get_mute(vol_ptr, ctypes.byref(muted)) == 0 and muted.value:
                                    set_mute(vol_ptr, 0, None)
                                lvl = ctypes.c_float()
                                if get_scalar(vol_ptr, ctypes.byref(lvl)) == 0 and lvl.value < 0.90:
                                    set_scalar(vol_ptr, ctypes.c_float(1.0), None)
                                vol_release(vol_ptr)

                        dev_release(dev_ptr)
        finally:
            enum_release(enum_ptr)
        return ("|".join(cap_ids), "|".join(ren_ids))
    except Exception:
        return ("", "")


def refresh_audio_devices() -> None:
    """Re-initialize PortAudio so hot-plugged Windows mics/headphones are detected."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def get_wasapi_default_device(kind: str) -> int | None:
    """Return the Windows WASAPI default input/output device index if available."""
    try:
        for host in sd.query_hostapis():
            if "wasapi" in str(host.get("name", "")).lower():
                key = "default_input_device" if kind == "input" else "default_output_device"
                idx = int(host.get(key, -1))
                if idx >= 0:
                    return idx
    except Exception:
        pass
    return None


def list_input_devices() -> list[dict[str, Any]]:
    """Return available audio input devices with host API labels."""
    devices: list[dict[str, Any]] = []
    try:
        wasapi_in = get_wasapi_default_device("input")
        default_in = (
            wasapi_in
            if wasapi_in is not None
            else (
                sd.default.device[0]
                if isinstance(sd.default.device, (list, tuple))
                else sd.default.device
            )
        )
        for idx, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) <= 0:
                continue
            hostapi_name = ""
            try:
                hostapi_name = str(sd.query_hostapis(int(info["hostapi"]))["name"])
            except Exception:
                pass
            devices.append(
                {
                    "index": idx,
                    "name": str(info.get("name", f"Device {idx}")),
                    "hostapi": hostapi_name,
                    "is_default": idx == default_in,
                }
            )
    except Exception:
        pass
    return devices


class AudioInputStream:
    """Captures 16 kHz mono int16 PCM frames with high-pass filtering and adaptive AGC."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        blocksize: int = 1280,
        max_queue_frames: int = 100,
        device: int | str | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.max_queue_frames = max_queue_frames
        self.device = device
        self.last_peak: int = 0
        self._capture_sample_rate: int = sample_rate
        self._noise_rms: float = 45.0
        self._agc_gain: float = 3.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._stream: sd.InputStream | None = None

    @staticmethod
    def _resolve_device(device: int | str | None) -> int | None:
        if device is None or device == "":
            return None
        if isinstance(device, int):
            return device
        text = str(device).strip()
        if text.isdigit():
            return int(text)
        needle = text.lower()
        for idx, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels", 0)) > 0 and needle in str(info.get("name", "")).lower():
                return idx
        return None

    def _preprocess_and_agc(self, raw_chunk: np.ndarray) -> np.ndarray:
        """Remove sub-100Hz fan/power hum and apply adaptive software AGC for laptop mics."""
        if len(raw_chunk) < 4:
            return raw_chunk.astype(np.int16)

        sig = raw_chunk.astype(np.float32)
        sig -= float(np.mean(sig))

        # Fast FFT band-pass (100 Hz - 7600 Hz) to strip laptop fan rumble and DC drift
        freqs = np.fft.rfftfreq(len(sig), d=1.0 / self.sample_rate)
        spec = np.fft.rfft(sig)
        mask = np.clip((freqs - 85.0) / 45.0, 0.0, 1.0) * np.clip((7700.0 - freqs) / 300.0, 0.0, 1.0)
        filtered = np.fft.irfft(spec * mask, n=len(sig))

        frame_rms = float(np.sqrt(np.mean(filtered**2)))
        frame_peak = float(np.max(np.abs(filtered)))

        if frame_rms < self._noise_rms * 1.35:
            self._noise_rms = float(np.clip(0.92 * self._noise_rms + 0.08 * frame_rms, 12.0, 220.0))

        speech_gate = max(110.0, self._noise_rms * 1.9)
        if frame_peak > speech_gate:
            desired_gain = float(np.clip(11500.0 / max(1.0, frame_peak), 1.0, 12.0))
            self._agc_gain = 0.40 * self._agc_gain + 0.60 * desired_gain
            boosted = np.clip(filtered * self._agc_gain, -32768.0, 32767.0).astype(np.int16)
            self.last_peak = int(np.max(np.abs(boosted)))
            return boosted

        self._agc_gain = max(1.5, self._agc_gain * 0.92)
        quiet = np.clip(filtered * 1.5, -32768.0, 32767.0).astype(np.int16)
        self.last_peak = int(min(400, frame_peak))
        return quiet

    def _callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if self._loop is None or self._queue is None:
            return
        if indata.ndim > 1 and indata.shape[1] > 1:
            # Pick the channel with strongest signal energy (handles asymmetric laptop mic arrays)
            ch_peaks = [int(np.max(np.abs(indata[:, c]))) for c in range(indata.shape[1])]
            best_ch = int(np.argmax(ch_peaks))
            chunk = indata[:, best_ch].copy()
        else:
            chunk = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()

        if self._capture_sample_rate != self.sample_rate and len(chunk) > 1:
            target_len = max(1, int(round(len(chunk) * self.sample_rate / self._capture_sample_rate)))
            x_old = np.linspace(0.0, 1.0, num=len(chunk), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            chunk = np.interp(x_new, x_old, chunk.astype(np.float32)).astype(np.int16)

        processed = self._preprocess_and_agc(chunk)
        self._loop.call_soon_threadsafe(self._enqueue_nonblocking, processed)

    @property
    def level_percent(self) -> int:
        """Return a 0-100 visual microphone level scaled for human speech."""
        if self.last_peak <= 80:
            return 0
        return min(100, max(1, int(round((self.last_peak / 9000.0) * 100))))

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

    def _open_portaudio_stream(self) -> sd.InputStream:
        """Open the best available input stream for the active Windows default microphone."""
        get_windows_default_endpoints(ensure_mic_unmuted=True)
        dev_idx = self._resolve_device(self.device)
        wasapi_idx = get_wasapi_default_device("input") if dev_idx is None else dev_idx

        # 1. Try Windows WASAPI default input with auto_convert=True (native kernel 16kHz conversion)
        if wasapi_idx is not None and hasattr(sd, "WasapiSettings"):
            try:
                self._capture_sample_rate = self.sample_rate
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    blocksize=self.blocksize,
                    device=wasapi_idx,
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                    callback=self._callback,
                )
                stream.start()
                return stream
            except Exception:
                pass

        # 2. Try standard MME / default input at 16 kHz
        try:
            self._capture_sample_rate = self.sample_rate
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.blocksize,
                device=dev_idx,
                callback=self._callback,
            )
            stream.start()
            return stream
        except sd.PortAudioError:
            target = wasapi_idx if wasapi_idx is not None else dev_idx
            try:
                info = sd.query_devices(target, kind="input")
            except Exception:
                target = None
                info = sd.query_devices(None, kind="input")
            native_sr = int(round(float(info.get("default_samplerate", 44100))))
            if native_sr <= 0:
                native_sr = 44100
            self._capture_sample_rate = native_sr
            scaled_block = max(256, int(round(self.blocksize * native_sr / self.sample_rate)))
            stream = sd.InputStream(
                samplerate=native_sr,
                channels=self.channels,
                dtype="int16",
                blocksize=scaled_block,
                device=target,
                callback=self._callback,
            )
            stream.start()
            return stream

    def start(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[np.ndarray]:
        """Start capturing microphone audio and return the frame queue."""
        if self._stream is not None:
            raise RuntimeError("AudioInputStream is already started.")
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=self.max_queue_frames)
        self._stream = self._open_portaudio_stream()
        return self._queue

    def reopen_on_default(self) -> None:
        """Hot-swap the underlying PortAudio input stream when Windows changes default mic."""
        if self._loop is None or self._queue is None:
            return
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stream = self._open_portaudio_stream()

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
    """Streams 24 kHz mono int16 PCM chunks to the active Windows default speaker/headphones."""

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

    def _open_portaudio_stream(self) -> sd.OutputStream:
        wasapi_idx = get_wasapi_default_device("output")
        if wasapi_idx is not None and hasattr(sd, "WasapiSettings"):
            try:
                stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    device=wasapi_idx,
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                    callback=self._callback,
                )
                stream.start()
                return stream
            except Exception:
                pass

        stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
        )
        stream.start()
        return stream

    def start(self) -> None:
        """Open and start the speaker output stream."""
        if self._stream is not None:
            return
        self._stream = self._open_portaudio_stream()

    def reopen_on_default(self) -> None:
        """Hot-swap the speaker output stream when Windows changes default output device."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stream = self._open_portaudio_stream()

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
