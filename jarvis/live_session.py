"""Gemini Live API session manager for Jarvis."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
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
- Keep spoken responses crisp, natural, and conversational — avoid reading long file paths, raw JSON, or huge terminal logs aloud unless asked; summarize key outcomes instead.
- You have asynchronous NON_BLOCKING tools for Windows desktop control, file management, reminders/memory, webhooks, code search, git inspection, and running tests.
- When the user mentions an outstanding task, follow-up, or reminder, save it proactively with `manage_reminders(action="add", ...)` so it stays synced across sessions.
- When the user shares a preference or recurring detail worth keeping, proactively save it with `remember_fact`.
- When a tool returns `status: "needs_confirmation"`, ask the user clearly for confirmation before re-invoking it with `confirmed=true` or `overwrite=true`.
- If you are unsure about a destructive change, ask first.
"""

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

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        res = self._on_event(payload)
        if inspect.isawaitable(res):
            await res

    def build_system_instruction(self) -> str:
        """Combine base persona with persisted memory facts and open reminders."""
        memory_context = self.memory.build_context_summary()
        parts = [
            _BASE_SYSTEM_INSTRUCTION.strip(),
            f"Current workspace directory: {self.config.workspace_dir}",
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
        status = str(result.get("status", "ok"))
        print(f"[tool] <- {call.name}: {status}")
        await self._emit({"type": "tool", "phase": "done", "name": call.name, "status": status})
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
    ) -> str:
        """Run one bidirectional audio session until idle timeout, GoAway, or cancellation."""
        live_config = self.build_live_config(text_only=False)
        last_activity = time.monotonic()
        interaction_status = "IDLE"
        active_tasks: set[asyncio.Task[None]] = set()
        end_reason = "closed"

        await self._emit({"type": "state", "state": "listening"})

        async with self._client.aio.live.connect(
            model=self.config.model,
            config=live_config,
        ) as session:
            if initial_prompt:
                self.memory.log_message("user", initial_prompt, source="desktop")
                await self._emit({"type": "transcript", "role": "user", "text": initial_prompt, "final": True})
                await session.send_realtime_input(text=initial_prompt)

            async def _send_mic_loop() -> None:
                nonlocal last_activity
                was_muted = False
                while True:
                    frame = await mic_queue.get()
                    if self.config.mute_mic_while_speaking and audio_out.is_playing:
                        was_muted = True
                        continue
                    if was_muted:
                        was_muted = False
                        audio_in.drain()
                        continue
                    if float(np.abs(frame).mean()) > 350.0:
                        last_activity = time.monotonic()
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=frame.tobytes(),
                            mime_type=f"audio/pcm;rate={self.config.input_sample_rate}",
                        )
                    )

            async def _send_text_loop() -> None:
                nonlocal last_activity
                if outbound_text_queue is None:
                    return
                while True:
                    text = await outbound_text_queue.get()
                    if not text.strip():
                        continue
                    last_activity = time.monotonic()
                    self.memory.log_message("user", text, source="desktop")
                    await self._emit({"type": "transcript", "role": "user", "text": text, "final": True})
                    await session.send_realtime_input(text=text)

            async def _receive_loop() -> None:
                nonlocal last_activity, interaction_status, end_reason
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
                        last_activity = time.monotonic()

                        status = _extract_interaction_status(message)
                        if status is not None and status != "INTERACTION_STATUS_UNSPECIFIED":
                            interaction_status = status

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

                            if content.model_turn and content.model_turn.parts:
                                for part in content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        audio_out.enqueue(part.inline_data.data)
                                        await self._emit({"type": "state", "state": "speaking"})

                            if content.input_transcription and content.input_transcription.text:
                                await _print_tx("You", content.input_transcription.text)

                            if content.output_transcription and content.output_transcription.text:
                                await _print_tx("Jarvis", content.output_transcription.text)

                            if content.turn_complete:
                                await _flush_speaker(active_speaker)
                                await self._emit({"type": "state", "state": "listening"})

                        if message.tool_call and message.tool_call.function_calls:
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
                    if (
                        interaction_status == "IDLE"
                        and not audio_out.is_playing
                        and not active_tasks
                        and (time.monotonic() - last_activity) >= idle_timeout_sec
                    ):
                        end_reason = "idle_timeout"
                        return

            send_task = asyncio.create_task(_send_mic_loop())
            recv_task = asyncio.create_task(_receive_loop())
            idle_task = asyncio.create_task(_idle_watchdog())
            text_task = asyncio.create_task(_send_text_loop())

            tasks = [send_task, recv_task, text_task]
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
