"""Local wake word detection using openWakeWord with anti-noise multi-frame confirmation."""

from __future__ import annotations

import numpy as np


class WakeWordDetector:
    """Detects a wake word (default: 'hey_jarvis') on 16 kHz 1280-sample int16 frames."""

    def __init__(
        self,
        model_name: str = "hey_jarvis",
        threshold: float = 0.50,
        consecutive_frames: int = 2,
        instant_threshold: float = 0.52,
        min_voice_rms: float = 85.0,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.consecutive_frames = max(1, int(consecutive_frames))
        self.instant_threshold = instant_threshold
        self.min_voice_rms = min_voice_rms
        self._model = None
        self._hit_streak: int = 0

    def load(self) -> None:
        """Load the openWakeWord ONNX model, downloading pretrained weights if needed."""
        import openwakeword
        from openwakeword.model import Model

        try:
            self._model = Model(
                wakeword_models=[self.model_name],
                inference_framework="onnx",
            )
        except Exception:
            # Download default pretrained models on first run, then retry
            openwakeword.utils.download_models([self.model_name])
            self._model = Model(
                wakeword_models=[self.model_name],
                inference_framework="onnx",
            )

    def process_frame(
        self,
        frame_int16: np.ndarray,
        *,
        speech_active: bool = True,
    ) -> tuple[bool, float]:
        """Score a single 80ms (1280-sample) int16 frame with multi-frame hysteresis.

        When `speech_active` is False or frame energy is below `min_voice_rms`,
        zeros are fed to flush internal ring buffers and avoid false positives from
        ambient room clicks or fan noise.
        """
        if self._model is None:
            self.load()
        assert self._model is not None

        frame_rms = float(np.sqrt(np.mean(frame_int16.astype(np.float32) ** 2))) if len(frame_int16) else 0.0
        has_vocal_energy = speech_active and frame_rms >= self.min_voice_rms

        if not has_vocal_energy:
            self._hit_streak = 0
            self._model.predict(np.zeros_like(frame_int16, dtype=np.int16))
            return False, 0.0

        scores = self._model.predict(frame_int16)
        if not scores:
            self._hit_streak = 0
            return False, 0.0

        # openWakeWord may key by full model filename (e.g. 'hey_jarvis_v0.1')
        best_score = max(float(v) for v in scores.values())

        if best_score >= self.threshold:
            self._hit_streak += 1
        elif best_score < (self.threshold * 0.70):
            self._hit_streak = 0

        detected = (
            self._hit_streak >= self.consecutive_frames
            or best_score >= self.instant_threshold
        )
        return detected, best_score

    def reset(self) -> None:
        """Reset internal ring buffers and confirmation streak after a trigger or cooldown."""
        self._hit_streak = 0
        if self._model is not None:
            self._model.reset()
