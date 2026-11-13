"""CLI entrypoint for Jarvis voice automation assistant."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys

from jarvis.audio import AudioInputStream, AudioOutputStream
from jarvis.config import JarvisConfig
from jarvis.live_session import JarvisLiveSession
from jarvis.memory import JarvisMemory
from jarvis.tools import build_default_registry
from jarvis.wake_word import WakeWordDetector


def run_self_check(config: JarvisConfig) -> int:
    """Verify dependencies, audio devices, memory DB, and tool declarations."""
    print(f"Workspace: {config.workspace_dir}")
    print(f"Model: {config.model}")
    print(f"API key configured: {'yes' if bool(config.gemini_api_key) else 'no (set GEMINI_API_KEY in .env)'}")

    for pkg in ("google.genai", "sounddevice", "numpy", "openwakeword", "onnxruntime", "httpx", "psutil"):
        found = importlib.util.find_spec(pkg) is not None
        print(f"Package {pkg}: {'ok' if found else 'MISSING'}")

    try:
        import sounddevice as sd

        in_dev = sd.query_devices(kind="input")
        out_dev = sd.query_devices(kind="output")
        print(f"Default mic: {in_dev['name']}")
        print(f"Default speaker: {out_dev['name']}")
    except Exception as exc:
        print(f"Audio device check warning: {exc}")

    memory = JarvisMemory(config.db_path)
    registry = build_default_registry(config, memory)
    tool = registry.to_gemini_tool()
    print(f"Registered tools ({len(registry.names())}): {', '.join(registry.names())}")
    print(f"Gemini declarations built: {len(tool.function_declarations or [])}")
    return 0


async def run_wakeword_loop(
    config: JarvisConfig,
    session_mgr: JarvisLiveSession,
) -> None:
    """Listen locally with openWakeWord, then connect to Gemini Live on wake word."""
    detector = WakeWordDetector(
        model_name=config.wake_word_model,
        threshold=config.wake_threshold,
    )
    print(f"Loading wake word model '{config.wake_word_model}'...")
    await asyncio.to_thread(detector.load)

    audio_in = AudioInputStream(
        sample_rate=config.input_sample_rate,
        channels=config.audio_channels,
        blocksize=config.input_chunk_samples,
    )
    audio_out = AudioOutputStream(
        sample_rate=config.output_sample_rate,
        channels=config.audio_channels,
    )

    loop = asyncio.get_running_loop()
    mic_queue = audio_in.start(loop)
    audio_out.start()

    print(f"Listening locally for wake word ('{config.wake_word_model}'). Press Ctrl+C to exit.")
    try:
        while True:
            frame = await mic_queue.get()
            detected, score = detector.process_frame(frame)
            if not detected:
                continue

            print(f"\n[wake] Detected '{config.wake_word_model}' (score={score:.2f}). Connecting to Gemini Live...")
            detector.reset()
            audio_in.drain()

            reason = await session_mgr.run_voice_session(
                audio_in=audio_in,
                audio_out=audio_out,
                mic_queue=mic_queue,
                idle_timeout_sec=config.idle_timeout_sec,
            )
            print(f"[wake] Session ended ({reason}). Returning to local wake word listening.")
            detector.reset()
            audio_in.drain()
    finally:
        audio_in.stop()
        audio_out.stop()


async def run_direct_voice(
    config: JarvisConfig,
    session_mgr: JarvisLiveSession,
) -> None:
    """Connect immediately to Gemini Live without wake word gating."""
    audio_in = AudioInputStream(
        sample_rate=config.input_sample_rate,
        channels=config.audio_channels,
        blocksize=config.input_chunk_samples,
    )
    audio_out = AudioOutputStream(
        sample_rate=config.output_sample_rate,
        channels=config.audio_channels,
    )

    loop = asyncio.get_running_loop()
    mic_queue = audio_in.start(loop)
    audio_out.start()

    print("Direct voice session active. Speak anytime (Ctrl+C to exit).")
    try:
        while True:
            reason = await session_mgr.run_voice_session(
                audio_in=audio_in,
                audio_out=audio_out,
                mic_queue=mic_queue,
                idle_timeout_sec=None,
            )
            if reason == "go_away":
                print("[live] Server requested reconnect; resuming session...")
                continue
            break
    finally:
        audio_in.stop()
        audio_out.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Jarvis Windows Voice Automation Assistant")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify environment, audio devices, memory DB, and tools without connecting.",
    )
    parser.add_argument(
        "--no-wakeword",
        action="store_true",
        help="Skip local wake word detection and start an active Live API voice session immediately.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Run in terminal text mode against Gemini Live API (no microphone required).",
    )
    parser.add_argument(
        "--barge-in",
        action="store_true",
        help="Keep microphone active while Jarvis speaks (recommended only with headphones).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = JarvisConfig.from_env()
    if args.barge_in:
        config.mute_mic_while_speaking = False

    if args.check:
        return run_self_check(config)

    try:
        config.validate_for_live()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    memory = JarvisMemory(config.db_path)
    registry = build_default_registry(config, memory)
    session_mgr = JarvisLiveSession(config=config, memory=memory, registry=registry)

    try:
        if args.text:
            asyncio.run(session_mgr.run_text_session())
        elif args.no_wakeword:
            asyncio.run(run_direct_voice(config, session_mgr))
        else:
            asyncio.run(run_wakeword_loop(config, session_mgr))
    except KeyboardInterrupt:
        print("\nJarvis stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
