"""Configuration management for Jarvis."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class JarvisConfig:
    """Runtime configuration loaded from environment variables and `.env`."""

    gemini_api_key: str = ""
    model: str = "gemini-3.8-live"
    thinking_level: str = "low"
    voice_name: str = "Fenrir"

    # Audio settings (Gemini Live API requires 16kHz PCM in / 24kHz PCM out)
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    audio_channels: int = 1
    # 1280 samples @ 16kHz = 80ms, matching openWakeWord's native frame size
    input_chunk_samples: int = 1280

    # Wake word & session lifecycle
    wake_word_model: str = "hey_jarvis"
    wake_threshold: float = 0.50
    wake_consecutive_frames: int = 2
    idle_timeout_sec: float = 15.0

    # Full-duplex VAD & barge-in conversation controls
    silence_duration_ms: int = 1100
    prefix_padding_ms: int = 300
    barge_in_enabled: bool = True

    # Storage & workspace
    data_dir: Path = field(default_factory=lambda: Path("data"))
    db_path: Path = field(default_factory=lambda: Path("data/jarvis_memory.db"))
    workspace_dir: Path = field(default_factory=Path.cwd)

    # Integrations & safety
    webhooks: dict[str, str] = field(default_factory=dict)
    auto_approve_dangerous: bool = False
    mute_mic_while_speaking: bool = False
    input_device: str = ""

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> JarvisConfig:
        """Load configuration from `.env` (if present) and environment variables."""
        raw_data_dir = os.getenv("JARVIS_DATA_DIR", "").strip()
        data_dir = Path(raw_data_dir).expanduser().resolve() if raw_data_dir else (Path.cwd() / "data")

        if env_file:
            load_dotenv(env_file, override=True)
        else:
            data_env = data_dir / ".env"
            if data_env.exists():
                load_dotenv(data_env, override=True)
            else:
                load_dotenv()

        raw_webhooks = os.getenv("JARVIS_WEBHOOKS", "{}").strip() or "{}"
        try:
            parsed_webhooks = json.loads(raw_webhooks)
            if not isinstance(parsed_webhooks, dict):
                parsed_webhooks = {}
        except json.JSONDecodeError:
            parsed_webhooks = {}

        workspace = Path(os.getenv("JARVIS_WORKSPACE_DIR", ".")).expanduser().resolve()
        raw_db_path = os.getenv("JARVIS_DB_PATH", "").strip()
        if raw_db_path:
            db_path = Path(raw_db_path).expanduser()
            if not db_path.is_absolute():
                db_path = Path.cwd() / db_path
        else:
            db_path = data_dir / "jarvis_memory.db"

        return cls(
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            model=os.getenv("JARVIS_MODEL", "gemini-3.8-live").strip(),
            thinking_level=os.getenv("JARVIS_THINKING_LEVEL", "low").strip(),
            voice_name=os.getenv("JARVIS_VOICE_NAME", "Fenrir").strip(),
            wake_word_model=os.getenv("JARVIS_WAKE_WORD", "hey_jarvis").strip(),
            wake_threshold=float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.50")),
            wake_consecutive_frames=max(1, int(os.getenv("JARVIS_WAKE_CONSECUTIVE_FRAMES", "2"))),
            idle_timeout_sec=float(os.getenv("JARVIS_IDLE_TIMEOUT_SEC", "15")),
            silence_duration_ms=int(os.getenv("JARVIS_SILENCE_DURATION_MS", "1100")),
            prefix_padding_ms=int(os.getenv("JARVIS_PREFIX_PADDING_MS", "300")),
            barge_in_enabled=os.getenv("JARVIS_BARGE_IN_ENABLED", "true").lower() == "true",
            data_dir=data_dir,
            db_path=db_path,
            workspace_dir=workspace,
            webhooks={str(k): str(v) for k, v in parsed_webhooks.items()},
            auto_approve_dangerous=os.getenv("JARVIS_AUTO_APPROVE_DANGEROUS", "false").lower() == "true",
            mute_mic_while_speaking=os.getenv("JARVIS_MUTE_WHILE_SPEAKING", "false").lower() == "true",
            input_device="",
        )

    @property
    def env_file_path(self) -> Path:
        """Preferred `.env` file path for persisting settings (data_dir when packaged)."""
        if os.getenv("JARVIS_DATA_DIR", "").strip():
            return self.data_dir / ".env"
        return Path.cwd() / ".env"

    def _save_env_var(self, key: str, value: str) -> Path:
        """Persist a key=value pair to the active `.env` file."""
        target = self.env_file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        updated = False
        new_lines: list[str] = []
        prefix = f"{key}="
        for line in lines:
            if line.strip().startswith(prefix):
                new_lines.append(f"{key}={value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}")
        target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return target

    def save_api_key(self, api_key: str) -> Path:
        """Persist `GEMINI_API_KEY` to the active `.env` file and update config in-memory."""
        cleaned = api_key.strip()
        target = self._save_env_var("GEMINI_API_KEY", cleaned)
        self.gemini_api_key = cleaned
        return target

    def save_input_device(self, device: str) -> Path:
        """Persist `JARVIS_INPUT_DEVICE` to the active `.env` file and update config in-memory."""
        cleaned = device.strip()
        target = self._save_env_var("JARVIS_INPUT_DEVICE", cleaned)
        self.input_device = cleaned
        return target

    def validate_for_live(self) -> None:
        """Ensure required credentials and sensible values exist before connecting."""
        if not self.gemini_api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to your .env file or environment variables."
            )
