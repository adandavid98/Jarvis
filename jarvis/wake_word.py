"""Local wake word detection using openWakeWord."""

from __future__ import annotations

import numpy as np


class WakeWordDetector:
    """Detects a wake word (default: 'hey_jarvis') on 16 kHz 1280-sample int16 frames."""

    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.18) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self._model = None

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

    def process_frame(self, frame_int16: np.ndarray) -> tuple[bool, float]:
        """Score a single 80ms (1280-sample) int16 frame.

        Returns:
            Tuple of (detected, confidence_score).
        """
        if self._model is None:
            self.load()
        assert self._model is not None

        scores = self._model.predict(frame_int16)
        if not scores:
            return False, 0.0

        # openWakeWord may key by full model filename (e.g. 'hey_jarvis_v0.1')
        best_score = max(float(v) for v in scores.values())
        return best_score >= self.threshold, best_score

    def reset(self) -> None:
        """Reset internal ring buffers after a wake word trigger."""
        if self._model is not None:
            self._model.reset()
