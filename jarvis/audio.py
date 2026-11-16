"""Windows-friendly bidirectional PCM audio I/O for Jarvis with auto-device hot-swap and AGC."""

from __future__ import annotations

import asyncio
import collections
import ctypes
import sys
import threading
import time
import uuid
from ctypes import wintypes
from typing import Any, Callable

import numpy as np
import sounddevice as sd
from scipy import signal


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


class PlaybackReferenceBuffer:
    """Thread-safe 16 kHz speaker playback ring buffer used as AEC reference for barge-in."""

    def __init__(self, target_rate: int = 16000, max_seconds: float = 3.0) -> None:
        self.target_rate = target_rate
        self.max_samples = int(target_rate * max_seconds)
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._last_sound_time: float = 0.0

    def push_24k(self, samples_24k: np.ndarray, source_rate: int = 24000) -> None:
        """Downsample played speaker samples to 16 kHz and append to reference ring buffer."""
        if len(samples_24k) == 0:
            return
        mono = samples_24k.astype(np.float32)
        if source_rate != self.target_rate and len(mono) > 1:
            target_len = max(1, int(round(len(mono) * self.target_rate / source_rate)))
            x_old = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            mono = np.interp(x_new, x_old, mono).astype(np.float32)
        if float(np.max(np.abs(mono))) > 35.0:
            self._last_sound_time = time.monotonic()
        with self._lock:
            combined = np.concatenate((self._buf, mono))
            if len(combined) > self.max_samples:
                combined = combined[-self.max_samples :]
            self._buf = combined

    def clear(self) -> None:
        """Reset playback reference state on barge-in interruption."""
        with self._lock:
            self._buf = np.zeros(0, dtype=np.float32)
            self._last_sound_time = 0.0

    @property
    def is_playing_echo_window(self) -> bool:
        """Return True while speaker audio played recently enough to create room echo."""
        return (time.monotonic() - self._last_sound_time) < 0.42

    def snapshot(self, max_samples: int = 8000) -> tuple[np.ndarray, bool]:
        """Return recent 16 kHz reference samples and whether echo window is active."""
        active = self.is_playing_echo_window
        with self._lock:
            if len(self._buf) == 0:
                return np.zeros(0, dtype=np.float32), active
            return self._buf[-max_samples:].copy(), active


class EchoCanceller:
    """Multi-tap delay-aligned Wiener/RIR echo canceller with double-talk barge-in detector."""

    _OFFSETS = np.concatenate((np.arange(-20, 21), np.arange(28, 220, 8))).astype(np.int64)

    def __init__(self, num_taps: int = 65, max_lag_samples: int = 4800) -> None:
        self.num_taps = int(len(self._OFFSETS)) if num_taps == 65 else num_taps
        self.max_lag_samples = max_lag_samples
        self._last_lag: int | None = None
        self._h: np.ndarray | None = None
        self._coupling: float = 0.30

    def reset(self) -> None:
        self._last_lag = None
        self._h = None
        self._coupling = 0.30

    def process(
        self,
        mic: np.ndarray,
        ref_hist: np.ndarray,
        *,
        noise_rms: float = 95.0,
    ) -> tuple[np.ndarray, bool]:
        """Cancel correlated speaker echo and report whether near-end user speech (double-talk) occurred."""
        n = len(mic)
        m = len(ref_hist)
        mic_z = mic.astype(np.float32) - float(np.mean(mic))
        mic_rms = float(np.sqrt(np.mean(mic_z**2))) if n else 0.0
        max_offset = int(self._OFFSETS[-1]) if self.num_taps == len(self._OFFSETS) else (self.num_taps // 2)

        if n < 64 or m < n + max_offset + 24:
            # While speaker playback buffer is still priming (<~100ms), only break on clear loud shouts
            return mic_z, mic_rms > max(1600.0, noise_rms * 4.5)

        ref_z = ref_hist.astype(np.float32) - float(np.mean(ref_hist))
        # Fast FFT cross-correlation across [0 .. max_lag_samples] speaker-to-mic acoustic delay
        fft_len = 1 << int(np.ceil(np.log2(m + n)))
        corr_full = np.fft.irfft(
            np.fft.rfft(ref_z, n=fft_len) * np.conj(np.fft.rfft(mic_z, n=fft_len)),
            n=fft_len,
        )
        max_s = m - n - max_offset - 2
        min_s = max(24, max_s - self.max_lag_samples)
        if min_s >= max_s:
            return mic_z, mic_rms > max(1600.0, noise_rms * 4.5)

        cs = corr_full[min_s : max_s + 1]
        c2 = np.concatenate(([0.0], np.cumsum(ref_z.astype(np.float64) ** 2)))
        ref_energies = np.maximum(1e-6, c2[min_s + n : max_s + 1 + n] - c2[min_s : max_s + 1])
        mic_energy = max(1e-6, float(np.sum(mic_z.astype(np.float64) ** 2)))
        norm_corr = np.abs(cs) / np.sqrt(ref_energies * mic_energy)

        rel_idx = int(np.argmax(norm_corr))
        best_rho = float(norm_corr[rel_idx])
        best_s = min_s + rel_idx
        best_lag = (m - n) - best_s
        aligned_ref_rms = float(np.sqrt(float(ref_energies[rel_idx]) / float(n)))

        offsets = (
            self._OFFSETS
            if self.num_taps == len(self._OFFSETS)
            else np.arange(-(self.num_taps // 2), (self.num_taps // 2) + 1, dtype=np.int64)
        )
        left_pad = int(abs(offsets[0]))
        center_s = max(left_pad, min(m - n - max_offset - 1, best_s))

        # Track speaker->mic acoustic coupling gain from reference windows
        if aligned_ref_rms > 250.0 and best_rho >= 0.25 and mic_rms < aligned_ref_rms * 1.1:
            inst_coupling = float(np.clip(mic_rms / aligned_ref_rms, 0.08, 0.85))
            if inst_coupling < self._coupling * 1.35:
                self._coupling = 0.85 * self._coupling + 0.15 * inst_coupling
        expected_echo_rms = aligned_ref_rms * self._coupling

        # Low correlation: either headphones OR dominant uncorrelated near-end speech
        if best_rho < 0.22:
            thresh = max(380.0, noise_rms * 3.1, expected_echo_rms * 1.35)
            return mic_z, mic_rms > thresh

        idx_mat = np.arange(n)[:, None] + (center_s + offsets)[None, :]
        x_mat = ref_z[idx_mat]

        reg = (0.04 * float(np.mean(x_mat**2)) + 1e-2) * np.eye(len(offsets), dtype=np.float32)
        try:
            h_est = np.linalg.solve(x_mat.T @ x_mat + reg, x_mat.T @ mic_z)
        except np.linalg.LinAlgError:
            return mic_z, False

        if self._h is None or len(self._h) != len(h_est) or self._last_lag is None or abs(best_lag - self._last_lag) > 24:
            self._h = h_est
        else:
            self._h = 0.60 * self._h + 0.40 * h_est
        self._last_lag = best_lag

        echo_hat = x_mat @ self._h
        residual = mic_z - echo_hat
        res_rms = float(np.sqrt(np.mean(residual**2)))
        echo_rms = float(np.sqrt(np.mean(echo_hat**2)))

        if best_rho >= 0.50:
            # Strong alignment (headphones leak or direct echo): near-end stands cleanly above FIR echo & noise
            double_talk = res_rms > max(260.0, noise_rms * 3.0, echo_rms * 0.78)
        else:
            # Reverberant laptop speaker echo (rho 0.22..0.50): require clear excursion above noise & coupling
            double_talk = res_rms > max(380.0, noise_rms * 3.1, echo_rms * 1.45, expected_echo_rms * 1.35)
        return residual.astype(np.float32), double_talk


class AudioInputStream:
    """Captures 16 kHz mono int16 PCM frames with Butterworth flat voice filtering, pre-roll, AEC & noise blocker."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        blocksize: int = 1280,
        max_queue_frames: int = 100,
        device: int | str | None = None,
        playback_ref: PlaybackReferenceBuffer | None = None,
        on_barge_in: Callable[[], None] | None = None,
        barge_in_enabled: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.max_queue_frames = max_queue_frames
        self.device = device
        self.playback_ref = playback_ref
        self.on_barge_in = on_barge_in
        self.barge_in_enabled = barge_in_enabled

        self.last_peak: int = 0
        self.is_speech_active: bool = False
        self._last_voice_level: int = 0
        self._capture_sample_rate: int = sample_rate
        self._capture_channels: int = channels

        # Dual-branch filter state: continuous 85 Hz Butterworth for Gemini + pre-emphasis for openWakeWord
        self._sos = signal.butter(2, 85.0, btype="highpass", fs=sample_rate, output="sos")
        self._hp_zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)
        self._prev_wake_sample: float = 0.0

        # Adaptive noise blocker, pre-roll (240ms = 3 frames), and speech hangover (1.2s = 15 frames)
        self._noise_rms: float = 95.0
        self._rms_history: collections.deque[float] = collections.deque(maxlen=40)
        self._env_gain: float = 1.0
        self._wake_env_rms: float = 250.0
        self._hangover: int = 0
        self._peak_streak: int = 0
        self._pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=3)
        self._pending_preroll: list[np.ndarray] = []
        self._wake_pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=3)
        self._pending_wake_preroll: list[np.ndarray] = []

        # Full-duplex AEC & barge-in confirmation (2 consecutive frames = 160ms)
        self._aec = EchoCanceller()
        self._barge_in_streak: int = 0

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._wake_queue: collections.deque[tuple[np.ndarray, bool]] = collections.deque(maxlen=max_queue_frames)
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

    def _downsample_to_16k(self, sig: np.ndarray) -> np.ndarray:
        """Convert mono float32 signal from capture rate to `self.sample_rate` (16 kHz)."""
        if self._capture_sample_rate == self.sample_rate * 3 and len(sig) >= 3:
            trim = (len(sig) // 3) * 3
            return ((sig[0 : trim - 2 : 3] + sig[1 : trim - 1 : 3] + sig[2:trim:3]) / 3.0).astype(np.float32)
        if self._capture_sample_rate != self.sample_rate and len(sig) > 1:
            target_len = max(1, int(round(len(sig) * self.sample_rate / self._capture_sample_rate)))
            x_old = np.linspace(0.0, 1.0, num=len(sig), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
            return np.interp(x_new, x_old, sig).astype(np.float32)
        return sig.astype(np.float32)

    def _process_dual_branch(self, raw_chunk: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
        """Process one capture block into `(gemini_voice_int16, wake_int16, speech_active)`."""
        if len(raw_chunk) < 4:
            pcm = raw_chunk.astype(np.int16)
            return pcm, pcm, False

        raw = raw_chunk.astype(np.float32)
        raw -= float(np.mean(raw))

        # Branch 1: Flat-passband 85 Hz Butterworth high-pass @ 16 kHz (natural vowels for Gemini ASR)
        sig16 = self._downsample_to_16k(raw)
        hp_vals, self._hp_zi = signal.sosfilt(self._sos, sig16.astype(np.float64), zi=self._hp_zi)
        hp_flat = hp_vals.astype(np.float32)

        # Branch 2: Rate-compensated pre-emphasis for openWakeWord sibilant sensitivity
        pre_scale = 3.0 if self._capture_sample_rate > 24000 else 3.8
        pe = np.empty_like(raw)
        pe[0] = (raw[0] - 0.94 * self._prev_wake_sample) * pre_scale
        pe[1:] = (raw[1:] - 0.94 * raw[:-1]) * pre_scale
        self._prev_wake_sample = float(raw[-1])
        wake16 = self._downsample_to_16k(pe)

        was_active = self.is_speech_active
        barge_in_now = False

        # Full-duplex Acoustic Echo Cancellation & Double-Talk Barge-In detection
        if self.playback_ref is not None:
            ref_hist, playing_echo = self.playback_ref.snapshot(max_samples=8000)
        else:
            ref_hist, playing_echo = np.zeros(0, dtype=np.float32), False

        if playing_echo:
            cleaned_hp, double_talk = self._aec.process(hp_flat, ref_hist, noise_rms=self._noise_rms)
            if double_talk:
                self._barge_in_streak += 1
            else:
                self._barge_in_streak = 0

            if self._barge_in_streak >= 2:
                barge_in_now = True
                hp_flat = cleaned_hp
                if self.barge_in_enabled and self.on_barge_in is not None:
                    try:
                        self.on_barge_in()
                    except Exception:
                        pass
            else:
                # Suppress pure speaker echo so Jarvis never interrupts itself or wastes API quota
                self._hangover = 0
                self._peak_streak = 0
                self._last_voice_level = 0
                self.last_peak = 0
                self.is_speech_active = False
                self._pre_roll.append(cleaned_hp)
                zeros = np.zeros(len(hp_flat), dtype=np.int16)
                return zeros, zeros, False
        else:
            self._barge_in_streak = 0

        f_rms = float(np.sqrt(np.mean(hp_flat**2)))
        f_pk = float(np.max(np.abs(hp_flat)))
        w_rms = float(np.sqrt(np.mean(wake16**2)))
        w_pk = float(np.max(np.abs(wake16)))

        # Adapt noise floor via rolling 20th-percentile (never locks on sustained room noise)
        if not barge_in_now:
            self._rms_history.append(f_rms)
            if len(self._rms_history) == 6:
                vals = list(self._rms_history)
                if max(vals) <= max(120.0, min(vals) * 2.4):
                    self._noise_rms = float(np.clip(float(np.median(vals)) * 1.05, 20.0, 1200.0))
                    self._hangover = 0
            elif len(self._rms_history) > 6:
                floor_p20 = float(np.percentile(self._rms_history, 20))
                if floor_p20 < self._noise_rms:
                    self._noise_rms = float(np.clip(0.75 * self._noise_rms + 0.25 * floor_p20, 20.0, 1200.0))
                elif floor_p20 > self._noise_rms and (
                    self._hangover == 0 or floor_p20 < self._noise_rms * 6.0
                ):
                    self._noise_rms = float(np.clip(0.82 * self._noise_rms + 0.18 * floor_p20, 20.0, 1200.0))
            elif self._hangover == 0 and f_rms < self._noise_rms * 1.25:
                self._noise_rms = float(np.clip(0.85 * self._noise_rms + 0.15 * f_rms, 20.0, 1200.0))

        is_sustained = barge_in_now or (f_rms > max(95.0, self._noise_rms * 1.75))
        has_spike = (f_pk > max(280.0, self._noise_rms * 6.0)) or (w_pk > max(280.0, self._noise_rms * 6.0))
        if has_spike and not is_sustained:
            self._peak_streak += 1
        elif is_sustained:
            self._peak_streak = 2
        else:
            self._peak_streak = 0

        is_voice = is_sustained or (self._peak_streak >= 2)
        if is_voice:
            self._hangover = 10  # 800ms hangover preserves clauses while letting idle timeout trigger reliably
            target_gain = float(np.clip(9500.0 / max(100.0, f_pk), 1.2, 9.0))
            self._env_gain = 0.45 * self._env_gain + 0.55 * target_gain
            excess = max(0.0, f_rms - self._noise_rms * 1.35)
            self._last_voice_level = min(
                100,
                max(15, int(round((excess / max(40.0, self._noise_rms * 1.8)) * 100))),
            )
        elif self._hangover > 0:
            self._hangover -= 1
            # Gentle hold during hangover so mid-sentence pauses aren't chopped
            self._env_gain = max(1.0, self._env_gain * 0.96)
            self._last_voice_level = max(0, int(self._last_voice_level * 0.72))
        else:
            self._env_gain = max(0.10, self._env_gain * 0.70)
            self._last_voice_level = 0

        active = is_voice or self._hangover > 0
        self.is_speech_active = active

        if active:
            if is_voice:
                clamped_w = float(np.clip(w_rms, 180.0, 2600.0))
                if clamped_w > self._wake_env_rms:
                    self._wake_env_rms = 0.85 * self._wake_env_rms + 0.15 * clamped_w
                else:
                    self._wake_env_rms = 0.96 * self._wake_env_rms + 0.04 * clamped_w
            w_slow_gain = float(np.clip(1800.0 / max(180.0, self._wake_env_rms), 1.0, 3.8))
            if not was_active and self._pre_roll:
                # Emit up to 240ms of pre-roll frames so initial consonants are never clipped
                roll_gain = max(1.2, min(4.5, self._env_gain * 0.8))
                self._pending_preroll = [
                    np.clip(buf * roll_gain, -32768.0, 32767.0).astype(np.int16)
                    for buf in self._pre_roll
                ]
                self._pending_wake_preroll = [
                    np.clip(buf * w_slow_gain, -32768.0, 32767.0).astype(np.int16)
                    for buf in self._wake_pre_roll
                ]
                self._pre_roll.clear()
                self._wake_pre_roll.clear()
            voice_out = np.clip(hp_flat * self._env_gain, -32768.0, 32767.0).astype(np.int16)
            wake_out = np.clip(wake16 * w_slow_gain, -32768.0, 32767.0).astype(np.int16)
            self.last_peak = int(np.max(np.abs(voice_out)))
        else:
            self._pre_roll.append(hp_flat)
            self._wake_pre_roll.append(wake16)
            self.last_peak = 0
            voice_out = np.zeros(len(hp_flat), dtype=np.int16)
            wake_out = voice_out

        return voice_out, wake_out, active

    def _preprocess_and_agc(self, raw_chunk: np.ndarray) -> np.ndarray:
        """Process one capture block into 16 kHz int16 voice PCM (compat helper & unit-test entry)."""
        voice_out, _, _ = self._process_dual_branch(raw_chunk)
        return voice_out

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
            # Pick the capsule with strongest AC standard deviation (ignores dead Ch 0 on laptop mic arrays)
            stds = [float(np.std(indata[:, c].astype(np.float32))) for c in range(indata.shape[1])]
            best_ch = int(np.argmax(stds))
            chunk = indata[:, best_ch].copy()
        else:
            chunk = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()

        voice_out, wake_out, active = self._process_dual_branch(chunk)
        preroll = self._pending_preroll
        wake_preroll = self._pending_wake_preroll
        self._pending_preroll = []
        self._pending_wake_preroll = []
        self._loop.call_soon_threadsafe(
            self._enqueue_batch, preroll, wake_preroll, voice_out, wake_out, active
        )

    @property
    def level_percent(self) -> int:
        """Return a 0-100 visual microphone level that responds only to human voice above ambient noise."""
        return self._last_voice_level

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

    def _enqueue_batch(
        self,
        preroll: list[np.ndarray],
        wake_preroll: list[np.ndarray],
        voice_out: np.ndarray,
        wake_out: np.ndarray,
        active: bool,
    ) -> None:
        for idx, pre in enumerate(preroll):
            self._enqueue_nonblocking(pre)
            w_pre = wake_preroll[idx] if idx < len(wake_preroll) else pre
            self._wake_queue.append((w_pre, True))
        self._enqueue_nonblocking(voice_out)
        self._wake_queue.append((wake_out, active))

    def reset_wake_state(self) -> None:
        """Reset wake envelope/hangover state when returning to wake-word standby."""
        self._wake_env_rms = 250.0
        self._hangover = 0
        self._peak_streak = 0
        self._last_voice_level = 0
        self.is_speech_active = False

    def pop_wake_frame(self) -> tuple[np.ndarray, bool] | None:
        """Pop the wake-word-optimized frame paired with the latest `mic_queue` item."""
        if not self._wake_queue:
            return None
        return self._wake_queue.popleft()

    def _reset_dsp_state(self) -> None:
        self._hp_zi = np.zeros((self._sos.shape[0], 2), dtype=np.float64)
        self._prev_wake_sample = 0.0
        self._noise_rms = 95.0
        self._rms_history.clear()
        self._env_gain = 1.0
        self._wake_env_rms = 250.0
        self._hangover = 0
        self._peak_streak = 0
        self._last_voice_level = 0
        self.last_peak = 0
        self.is_speech_active = False
        self._barge_in_streak = 0
        self._pre_roll.clear()
        self._pending_preroll.clear()
        self._wake_pre_roll.clear()
        self._pending_wake_preroll.clear()
        self._wake_queue.clear()
        self._aec.reset()

    def _open_portaudio_stream(self) -> sd.InputStream:
        """Open the best available input stream for the active Windows default microphone."""
        get_windows_default_endpoints(ensure_mic_unmuted=True)
        dev_idx = self._resolve_device(self.device)
        wasapi_idx = get_wasapi_default_device("input") if dev_idx is None else dev_idx
        self._reset_dsp_state()

        # 1. Try Windows WASAPI Exclusive mode at native sample rate & channels
        #    (Bypasses broken Realtek Shared-Mode FxProperties APO on laptop mic arrays)
        if wasapi_idx is not None and hasattr(sd, "WasapiSettings"):
            try:
                info = sd.query_devices(wasapi_idx, kind="input")
                native_sr = int(round(float(info.get("default_samplerate", 48000)))) or 48000
                max_ch = max(1, min(2, int(info.get("max_input_channels", 1))))
                scaled_block = max(256, int(round(self.blocksize * native_sr / self.sample_rate)))
                self._capture_sample_rate = native_sr
                self._capture_channels = max_ch
                stream = sd.InputStream(
                    samplerate=native_sr,
                    channels=max_ch,
                    dtype="int16",
                    blocksize=scaled_block,
                    device=wasapi_idx,
                    extra_settings=sd.WasapiSettings(exclusive=True),
                    callback=self._callback,
                )
                stream.start()
                return stream
            except Exception:
                pass

        # 2. Try Windows WASAPI Shared mode with auto_convert=True at 16kHz
        if wasapi_idx is not None and hasattr(sd, "WasapiSettings"):
            try:
                self._capture_sample_rate = self.sample_rate
                self._capture_channels = self.channels
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

        # 3. Try standard MME / default input at 16 kHz or native sample rate
        try:
            self._capture_sample_rate = self.sample_rate
            self._capture_channels = self.channels
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
            native_sr = int(round(float(info.get("default_samplerate", 44100)))) or 44100
            self._capture_sample_rate = native_sr
            self._capture_channels = self.channels
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
        self._wake_queue.clear()
        self._pending_preroll.clear()
        self._pre_roll.clear()
        self._pending_wake_preroll.clear()
        self._wake_pre_roll.clear()
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

    def __init__(
        self,
        sample_rate: int = 24000,
        channels: int = 1,
        playback_ref: PlaybackReferenceBuffer | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.playback_ref = playback_ref or PlaybackReferenceBuffer(target_rate=16000)
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
        if available > 0:
            self.playback_ref.push_24k(samples[:, 0], source_rate=self.sample_rate)

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
        self.playback_ref.clear()

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
