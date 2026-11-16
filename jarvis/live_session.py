"""Gemini Live API session manager for Jarvis."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import time
from typing import Any, Callable

import numpy as np
from google import genai
from google.genai import types

from jarvis.audio import AudioInputStream, AudioOutputStream
from jarvis.config import JarvisConfig
from jarvis.memory import JarvisMemory
from jarvis.tools.registry import ToolRegistry

_BASE_SYSTEM_INSTRUCTION = """You are Jarvis, a sharp, concise, proactive desktop and developer assistant running on Windows.

Guidelines:
- Always respond in the same language the user speaks — seamlessly supporting English, Spanish, and any other language they switch to.
- Execute requested work immediately using your tools (`create_document`, `write_file`, `edit_file`, `run_shell_command`, `search_code`, etc.). When asked to create a PDF, DOCX, or file, generate it directly on disk with `create_document` or `write_file` — do NOT read the entire document contents aloud.
- Keep spoken responses crisp and brief (1-2 sentences): confirm what was created or changed and where it was saved, instead of reading long text, file paths, raw JSON, or terminal logs aloud unless asked.
- You have asynchronous NON_BLOCKING tools for Windows desktop control, document/file management, PowerShell commands, reminders/memory, webhooks, code search, git inspection, and running tests.
- When the user mentions an outstanding task, follow-up, or reminder, save it proactively with `manage_reminders(action="add", ...)` so it stays synced across sessions.
- When the user shares a preference or recurring detail worth keeping, proactively save it with `remember_fact`.
- When a tool returns `status: "needs_confirmation"`, ask the user clearly for confirmation before re-invoking it with `confirmed=true` or `overwrite=true`.
- If you are unsure about a destructive change, ask first.
- NEVER call `enter_standby` unless the user explicitly asks to go to sleep, enter standby, pause/stop listening, or says goodbye — never call it on incomplete phrases or conversational pauses.
- Recent conversation history in system context is past-session reference only. Do NOT assume incomplete old fragments are still active unless the user resumes them, and if you only catch a tiny stray noise/word like "¿Qué?" or "y" after wake-up, just ask briefly how you can help instead of inventing an unrequested response.
"""

_STANDBY_PHRASE_RE = re.compile(
    r"\b("
    r"stand\s?by|go\s+to\s+sleep|sleep\s+mode|stop\s+listening|that'?s\s+all|bye|goodbye|"
    r"modo\s+espera|descansa|duerme|du[eé]rmete|vete\s+a\s+dormir|deja\s+de\s+escuchar|"
    r"eso\s+es\s+todo|nada\s+m[aá]s|adi[oó]s|chao|hasta\s+luego|nos\s+vemos|silencio|c[aá]llate"
    r")\b",
    re.IGNORECASE,
)


def _monotonic() -> float:
    return time.monotonic()


def _explicit_standby_requested(text: str) -> bool:
    """Return True only if `text` contains an explicit user command to enter standby/sleep."""
    return bool(_STANDBY_PHRASE_RE.search(text or ""))

EventCallback = Callable[[dict[str, Any]], Any]


def _extract_interaction_status(message: types.LiveServerMessage) -> str | None:
    """Read `interaction_status` from server_content (or top-level fallback) as a short string."""
    sc = getattr(message, "server_content", None)
    status = getattr(sc, "interaction_status", None) if sc is not None else None
    if status is None:
        status = getattr(message, "interaction_status", None)
    if status is None:
        return None
    value = getattr(status, "value", status)
    return str(value)


class JarvisLiveSession:
    """Manages a real-time Gemini Live API session (voice or text) with async tools."""

    def __init__(
        self,
        config: JarvisConfig,
        memory: JarvisMemory,
        registry: ToolRegistry,
        on_event: EventCallback | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.registry = registry
        self._on_event = on_event
        self._client = genai.Client(
            api_key=config.gemini_api_key or "UNSET",
            http_options={"api_version": "v1beta"},
        )
        self._resumption_handle: str | None = None
        self._standby_event: asyncio.Event | None = None
        self._recent_user_utterances: list[tuple[float, str]] = []

    def _record_user_utterance(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        self._recent_user_utterances.append((_monotonic(), cleaned))
        if len(self._recent_user_utterances) > 12:
            del self._recent_user_utterances[:-12]

    def request_standby(self) -> None:
        """Signal the active voice session to exit back to idle wake-word mode."""
        if self._standby_event is not None:
            self._standby_event.set()

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        res = self._on_event(payload)
        if inspect.isawaitable(res):
            await res

    def build_system_instruction(self) -> str:
        """Combine base persona with persisted memory facts and open reminders."""
        from pathlib import Path

        home = Path.home()
        memory_context = self.memory.build_context_summary()
        parts = [
            _BASE_SYSTEM_INSTRUCTION.strip(),
            (
                f"Current workspace directory: {self.config.workspace_dir}\n"
                f"User Downloads folder: {home / 'Downloads'}\n"
                f"User Desktop folder: {home / 'Desktop'}\n"
                f"User Documents folder: {home / 'Documents'}"
            ),
        ]
        if self.config.webhooks:
            parts.append(f"Configured webhook aliases: {', '.join(sorted(self.config.webhooks))}")
        if memory_context:
            parts.append(memory_context)
        return "\n\n".join(parts)

    def build_live_config(self, *, text_only: bool = False) -> types.LiveConnectConfig:
        """Build `LiveConnectConfig` tailored to Gemini 3.8 Live / Extended Thinking.

        Note: Gemini 3.8 Live models are native-audio models and always require
        `response_modalities=[types.Modality.AUDIO]`. In `--text` mode we enable
        `output_audio_transcription` and print transcripts without playing audio.
        """
        kwargs: dict[str, Any] = {
            "response_modalities": [types.Modality.AUDIO],
            "system_instruction": types.Content(
                parts=[types.Part(text=self.build_system_instruction())]
            ),
            "tools": [self.registry.to_gemini_tool()],
            "output_audio_transcription": types.AudioTranscriptionConfig(),
            "context_window_compression": types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            "session_resumption": types.SessionResumptionConfig(
                handle=self._resumption_handle
            ),
        }

        if not text_only:
            kwargs["speech_config"] = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.config.voice_name
                    )
                )
            )
            kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
            kwargs["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=self.config.prefix_padding_ms,
                    silence_duration_ms=self.config.silence_duration_ms,
                ),
                activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
            )

        # thinking_config is only supported on extended-thinking models
        if "extended-thinking" in self.config.model:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=self.config.thinking_level
            )

        return types.LiveConnectConfig(**kwargs)

    async def _execute_and_respond(self, session: Any, call: Any) -> None:
        args = dict(call.args) if call.args else {}
        print(f"\n[tool] -> {call.name}({args})")
        await self._emit({"type": "tool", "phase": "start", "name": call.name, "args": args})
        result = await self.registry.execute(call.name, args)
        if call.name == "enter_standby" and result.get("standby"):
            now = _monotonic()
            recent_text = " ".join(t for ts, t in self._recent_user_utterances if (now - ts) <= 20.0)
            if not _explicit_standby_requested(recent_text):
                result = {
                    "status": "rejected",
                    "standby": False,
                    "message": "User did not ask to enter standby. Stay in listening mode.",
                }
        status = str(result.get("status", "ok"))
        print(f"[tool] <- {call.name}: {status}")
        await self._emit(
            {
                "type": "tool",
                "phase": "done",
                "name": call.name,
                "status": status,
                "args": args,
                "result": result,
            }
        )
        if result.get("viewer_url"):
            await self._emit(
                {
                    "type": "open_viewer",
                    "url": str(result["viewer_url"]),
                    "title": str(result.get("title") or call.name),
                }
            )
        if result.get("standby") and self._standby_event is not None:
            self._standby_event.set()
        if call.name in {"remember_fact", "manage_reminders"}:
            await self._emit({"type": "memory_updated"})
        await session.send_tool_response(
            function_responses=[
                types.FunctionResponse(
                    id=call.id,
                    name=call.name,
                    response=result,
                )
            ]
        )

    async def run_voice_session(
        self,
        audio_in: AudioInputStream,
        audio_out: AudioOutputStream,
        mic_queue: asyncio.Queue[np.ndarray],
        *,
        idle_timeout_sec: float | None = None,
        initial_prompt: str | None = None,
        outbound_text_queue: asyncio.Queue[str] | None = None,
        wake_greeting: bool = False,
    ) -> str:
        """Run one bidirectional audio session until idle timeout, GoAway, or cancellation."""
        live_config = self.build_live_config(text_only=False)
        last_activity = _monotonic()
        last_meaningful_activity = last_activity
        interaction_status = "IDLE"
        turn_idle = True
        active_tasks: set[asyncio.Task[None]] = set()
        end_reason = "closed"
        self._recent_user_utterances.clear()

        await self._emit({"type": "state", "state": "listening"})

        standby_event = asyncio.Event()
        self._standby_event = standby_event
        greeting_cache_path = self.config.data_dir / "wake_greeting_24k.pcm"
        use_cached_greeting = (
            wake_greeting
            and not initial_prompt
            and greeting_cache_path.exists()
            and greeting_cache_path.stat().st_size > 8000
        )
        recording_greeting = bool(wake_greeting and not initial_prompt and not use_cached_greeting)
        greeting_pcm = bytearray()

        if wake_greeting and not initial_prompt:
            # Never inherit stale resumption context across separate wake-word activations
            self._resumption_handle = None

        async with self._client.aio.live.connect(
            model=self.config.model,
            config=live_config,
        ) as session:
            greeting_state_task: asyncio.Task[None] | None = None
            if initial_prompt:
                self._record_user_utterance(initial_prompt)
                self.memory.log_message("user", initial_prompt, source="desktop")
                await self._emit({"type": "transcript", "role": "user", "text": initial_prompt, "final": True})
                await session.send_realtime_input(text=initial_prompt)
            elif use_cached_greeting:
                audio_out.enqueue(greeting_cache_path.read_bytes())
                await self._emit({"type": "state", "state": "speaking"})
                await self._emit(
                    {"type": "transcript", "role": "jarvis", "text": "Hola, ¿cómo te ayudo?", "final": True}
                )
                last_activity = _monotonic()
                last_meaningful_activity = last_activity

                async def _reset_state_after_greeting() -> None:
                    nonlocal last_activity, last_meaningful_activity
                    while audio_out.is_playing:
                        await asyncio.sleep(0.1)
                    now = _monotonic()
                    last_activity = now
                    last_meaningful_activity = now
                    await self._emit({"type": "state", "state": "listening"})

                greeting_state_task = asyncio.create_task(_reset_state_after_greeting())
            elif wake_greeting:
                await session.send_realtime_input(
                    text=(
                        "[Sistema: El usuario acaba de activarte con 'Hey Jarvis'. "
                        "Responde una única vez diciendo exactamente 'Hola, ¿cómo te ayudo?' y quédate escuchando.]"
                    )
                )

            async def _send_mic_loop() -> None:
                nonlocal last_activity
                was_muted = False
                was_streaming_speech = False
                while True:
                    frame = await mic_queue.get()
                    if self.config.mute_mic_while_speaking and audio_out.is_playing:
                        was_muted = True
                        if was_streaming_speech:
                            was_streaming_speech = False
                            with contextlib.suppress(Exception):
                                await session.send_realtime_input(audio_stream_end=True)
                        continue
                    if was_muted:
                        was_muted = False
                        audio_in.drain()
                        continue

                    raw_flag = getattr(audio_in, "is_speech_active", True)
                    speech_active = raw_flag if isinstance(raw_flag, bool) else True

                    if speech_active:
                        was_streaming_speech = True
                        last_activity = _monotonic()
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=frame.tobytes(),
                                mime_type=f"audio/pcm;rate={self.config.input_sample_rate}",
                            )
                        )
                    elif was_streaming_speech:
                        was_streaming_speech = False
                        await session.send_realtime_input(audio_stream_end=True)

            async def _send_text_loop() -> None:
                nonlocal last_activity, last_meaningful_activity
                if outbound_text_queue is None:
                    return
                while True:
                    text = await outbound_text_queue.get()
                    if not text.strip():
                        continue
                    now = _monotonic()
                    last_activity = now
                    last_meaningful_activity = now
                    self._record_user_utterance(text)
                    self.memory.log_message("user", text, source="desktop")
                    await self._emit({"type": "transcript", "role": "user", "text": text, "final": True})
                    await session.send_realtime_input(text=text)

            async def _receive_loop() -> None:
                nonlocal last_activity, last_meaningful_activity, interaction_status, turn_idle, end_reason, recording_greeting
                active_speaker: str | None = None
                turn_buffers: dict[str, list[str]] = {"You": [], "Jarvis": []}

                async def _flush_speaker(speaker: str | None) -> None:
                    nonlocal active_speaker
                    if speaker is None:
                        return
                    full_text = "".join(turn_buffers[speaker]).strip()
                    turn_buffers[speaker].clear()
                    if full_text:
                        role = "user" if speaker == "You" else "jarvis"
                        self.memory.log_message(role, full_text, source="voice")
                        await self._emit({"type": "transcript", "role": role, "text": full_text, "final": True})
                    print()
                    active_speaker = None

                async def _print_tx(speaker: str, chunk: str) -> None:
                    nonlocal active_speaker
                    if active_speaker != speaker:
                        if active_speaker is not None:
                            await _flush_speaker(active_speaker)
                        print(f"{speaker}: ", end="", flush=True)
                        active_speaker = speaker
                    turn_buffers[speaker].append(chunk)
                    print(chunk, end="", flush=True)
                    role = "user" if speaker == "You" else "jarvis"
                    await self._emit(
                        {
                            "type": "transcript",
                            "role": role,
                            "text": "".join(turn_buffers[speaker]),
                            "final": False,
                        }
                    )

                while True:
                    received_any = False
                    async for message in session.receive():
                        received_any = True
                        last_activity = _monotonic()

                        status = _extract_interaction_status(message)
                        if status is not None and status != "INTERACTION_STATUS_UNSPECIFIED":
                            interaction_status = status
                            if status == "IDLE":
                                turn_idle = True

                        resumption_update = getattr(message, "session_resumption_update", None)
                        if resumption_update and getattr(resumption_update, "new_handle", None):
                            self._resumption_handle = resumption_update.new_handle

                        if getattr(message, "go_away", None) is not None:
                            await _flush_speaker(active_speaker)
                            end_reason = "go_away"
                            return

                        content = message.server_content
                        if content:
                            if content.interrupted is True:
                                audio_out.clear()
                                turn_idle = True
                                recording_greeting = False
                                now = _monotonic()
                                last_activity = now
                                last_meaningful_activity = now
                                await _flush_speaker(active_speaker)
                                await self._emit({"type": "state", "state": "listening"})

                            if content.model_turn and content.model_turn.parts:
                                turn_idle = False
                                now = _monotonic()
                                last_activity = now
                                last_meaningful_activity = now
                                for part in content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        if recording_greeting:
                                            greeting_pcm.extend(part.inline_data.data)
                                        audio_out.enqueue(part.inline_data.data)
                                        await self._emit({"type": "state", "state": "speaking"})

                            if content.input_transcription and content.input_transcription.text:
                                now = _monotonic()
                                last_activity = now
                                last_meaningful_activity = now
                                self._record_user_utterance(content.input_transcription.text)
                                await _print_tx("You", content.input_transcription.text)

                            if content.output_transcription and content.output_transcription.text:
                                now = _monotonic()
                                last_activity = now
                                last_meaningful_activity = now
                                await _print_tx("Jarvis", content.output_transcription.text)

                            if content.turn_complete:
                                turn_idle = True
                                now = _monotonic()
                                last_activity = now
                                last_meaningful_activity = now
                                if recording_greeting:
                                    recording_greeting = False
                                    if len(greeting_pcm) > 8000:
                                        greeting_cache_path.parent.mkdir(parents=True, exist_ok=True)
                                        greeting_cache_path.write_bytes(bytes(greeting_pcm))
                                await _flush_speaker(active_speaker)
                                await self._emit({"type": "state", "state": "listening"})

                        if message.tool_call and message.tool_call.function_calls:
                            turn_idle = False
                            now = _monotonic()
                            last_activity = now
                            last_meaningful_activity = now
                            await _flush_speaker(active_speaker)
                            await self._emit({"type": "state", "state": "tool_running"})
                            for call in message.tool_call.function_calls:
                                task = asyncio.create_task(self._execute_and_respond(session, call))
                                active_tasks.add(task)
                                task.add_done_callback(active_tasks.discard)

                    if not received_any:
                        await _flush_speaker(active_speaker)
                        return

            async def _idle_watchdog() -> None:
                nonlocal end_reason
                if not idle_timeout_sec or idle_timeout_sec <= 0:
                    return
                while True:
                    await asyncio.sleep(1.0)
                    now = _monotonic()
                    if (
                        (interaction_status == "IDLE" or turn_idle)
                        and not audio_out.is_playing
                        and not active_tasks
                        and (
                            (now - last_activity) >= idle_timeout_sec
                            or (now - last_meaningful_activity) >= (idle_timeout_sec + 5.0)
                        )
                    ):
                        end_reason = "idle_timeout"
                        return

            async def _standby_watchdog() -> None:
                nonlocal end_reason
                await standby_event.wait()
                # Let Jarvis finish a brief spoken farewell if audio is queued
                for _ in range(16):
                    if not audio_out.is_playing:
                        break
                    await asyncio.sleep(0.25)
                end_reason = "standby"

            send_task = asyncio.create_task(_send_mic_loop())
            recv_task = asyncio.create_task(_receive_loop())
            idle_task = asyncio.create_task(_idle_watchdog())
            standby_task = asyncio.create_task(_standby_watchdog())

            tasks: list[asyncio.Task[Any]] = [send_task, recv_task, standby_task]
            if outbound_text_queue is not None:
                tasks.append(asyncio.create_task(_send_text_loop()))
            if idle_timeout_sec and idle_timeout_sec > 0:
                tasks.append(idle_task)
            else:
                idle_task.cancel()

            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for task in done:
                    exc = task.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        raise exc
            except asyncio.CancelledError:
                end_reason = "cancelled"
                raise
            finally:
                if greeting_state_task is not None and not greeting_state_task.done():
                    greeting_state_task.cancel()
                self._standby_event = None
                if end_reason in {"cancelled", "standby", "idle_timeout"}:
                    self._resumption_handle = None
                for t in list(active_tasks):
                    t.cancel()
                audio_out.clear()

        return end_reason

    async def run_text_session(self) -> None:
        """Run an interactive terminal text chat against Gemini Live API with tools."""
        live_config = self.build_live_config(text_only=True)
        active_tasks: set[asyncio.Task[None]] = set()
        idle_event = asyncio.Event()
        idle_event.set()

        standby_event = asyncio.Event()
        self._standby_event = standby_event
        async with self._client.aio.live.connect(
            model=self.config.model,
            config=live_config,
        ) as session:
            async def _receive_loop() -> None:
                jarvis_chunks: list[str] = []
                while True:
                    received_any = False
                    async for message in session.receive():
                        received_any = True
                        status = _extract_interaction_status(message)
                        content = message.server_content
                        if content:
                            if content.output_transcription and content.output_transcription.text:
                                jarvis_chunks.append(content.output_transcription.text)
                                print(content.output_transcription.text, end="", flush=True)
                            if content.model_turn and content.model_turn.parts:
                                for part in content.model_turn.parts:
                                    if part.text:
                                        jarvis_chunks.append(part.text)
                                        print(part.text, end="", flush=True)

                        if message.tool_call and message.tool_call.function_calls:
                            for call in message.tool_call.function_calls:
                                task = asyncio.create_task(self._execute_and_respond(session, call))
                                active_tasks.add(task)
                                task.add_done_callback(active_tasks.discard)

                        is_idle = status == "IDLE" or (
                            "extended-thinking" not in self.config.model
                            and content is not None
                            and content.turn_complete is True
                            and not active_tasks
                        )
                        if is_idle:
                            if jarvis_chunks:
                                self.memory.log_message("jarvis", "".join(jarvis_chunks), source="text")
                                jarvis_chunks.clear()
                            print()
                            idle_event.set()

                    if not received_any:
                        return
                    if not idle_event.is_set() and not active_tasks:
                        if jarvis_chunks:
                            self.memory.log_message("jarvis", "".join(jarvis_chunks), source="text")
                            jarvis_chunks.clear()
                        print()
                        idle_event.set()

            recv_task = asyncio.create_task(_receive_loop())
            print("Text mode ready. Type a message (or 'exit' to quit).")
            try:
                while True:
                    user_text = await asyncio.to_thread(input, "\nYou> ")
                    if user_text.strip().lower() in {"exit", "quit", "/quit"}:
                        break
                    if not user_text.strip():
                        continue
                    self.memory.log_message("user", user_text, source="text")
                    idle_event.clear()
                    print("Jarvis> ", end="", flush=True)
                    await session.send_realtime_input(text=user_text)
                    await asyncio.wait_for(idle_event.wait(), timeout=60.0)
            finally:
                recv_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recv_task
                for t in list(active_tasks):
                    t.cancel()
