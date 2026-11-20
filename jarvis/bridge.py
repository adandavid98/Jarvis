"""Local WebSocket bridge connecting the Jarvis Electron Desktop UI to the Python core."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psutil
from websockets.asyncio.server import ServerConnection, serve

from jarvis.audio import (
    AudioInputStream,
    AudioOutputStream,
    get_windows_default_endpoints,
    list_input_devices,
    refresh_audio_devices,
)
from jarvis.config import JarvisConfig
from jarvis.live_session import JarvisLiveSession
from jarvis.memory import JarvisMemory
from jarvis.tools import build_default_registry
from jarvis.wake_word import WakeWordDetector


def _setup_bridge_logger(data_dir: Path) -> logging.Logger:
    """Create or reuse a rotating file logger under `<data_dir>/logs/bridge.log`."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "bridge.log").resolve()

    logger = logging.getLogger("jarvis.bridge")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    has_same_file = any(
        isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == log_path
        for h in logger.handlers
    )
    if not has_same_file:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


class JarvisBridge:
    """Hosts a local WebSocket server for the Desktop UI and manages Jarvis voice/text state."""

    def __init__(self, config: JarvisConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.config = config
        self.host = host
        self.port = port
        self.logger = _setup_bridge_logger(config.data_dir)
        self.memory = JarvisMemory(config.db_path)
        self.registry = build_default_registry(config, self.memory)
        self.session_mgr = JarvisLiveSession(
            config=config,
            memory=self.memory,
            registry=self.registry,
            on_event=self._on_session_event,
        )

        self._clients: set[ServerConnection] = set()
        self._state: str = "stopped"
        self._voice_task: asyncio.Task[None] | None = None
        self._outbound_text_queue: asyncio.Queue[str] | None = None
        os.environ.setdefault("JARVIS_EMBEDDED_VIEWER", "1")
        self._wake_event: asyncio.Event | None = None
        self._detector: WakeWordDetector | None = None
        self._telemetry_paused: bool = False
        self._active_task: dict[str, Any] | None = None
        self._pending_task_prompt: str = ""
        self._audio_in: AudioInputStream | None = None
        self._audio_out: AudioOutputStream | None = None
        self._default_endpoints: tuple[str, str] = ("", "")
        self._voice_mode: str = "wakeword"

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return
        raw = json.dumps(payload, default=str)
        dead: list[ServerConnection] = []
        for ws in list(self._clients):
            try:
                await ws.send(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _set_state(self, state: str) -> None:
        if state != self._state:
            self.logger.info("state=%s", state)
        self._state = state
        await self._broadcast({"type": "state", "state": state})

    @staticmethod
    def _format_tool_action(
        name: str,
        args: dict[str, Any],
        status: str = "running",
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        res = result or {}
        kind = "app_action"
        title = name
        target = ""
        if name == "web_search":
            kind = "web_search"
            q = args.get("query") or res.get("query") or ""
            title = f"Searched web: {q}" if q else "Web search"
            target = str(res.get("viewer_url") or "")
        elif name == "fetch_web_page":
            kind = "open_url"
            u = str(res.get("url") or args.get("url") or "")
            title = f"Read web page: {res.get('title') or u}"
            target = str(res.get("viewer_url") or u)
        elif name == "read_pdf":
            kind = "file_edit"
            p = str(res.get("path") or args.get("path") or "")
            title = f"Read PDF: {p}"
            target = p
        elif name == "run_coding_agent":
            kind = "command"
            t = str(args.get("task") or "")
            changed = res.get("files_changed") or []
            suffix = f" ({len(changed)} files)" if changed else ""
            title = f"Coding Agent{suffix}: {t[:65]}"
        elif name == "open_application_or_url":
            raw_target = str(args.get("target") or res.get("opened") or "")
            if raw_target.lower().startswith(("http://", "https://")) or res.get("viewer_url"):
                kind = "open_url"
                title = f"Opened URL: {raw_target}"
                target = str(res.get("viewer_url") or raw_target)
            else:
                title = f"Opened {raw_target}"
                target = raw_target
        elif name in {"write_file", "edit_file", "create_document"}:
            kind = "file_edit"
            path = str(res.get("path") or args.get("path") or "")
            verb = "Created document" if name == "create_document" else "Updated file"
            title = f"{verb}: {path}"
            target = path
        elif name == "run_shell_command":
            kind = "command"
            cmd = str(args.get("command") or "")
            title = f"Ran command: {cmd[:70]}"
        elif name in {"remember_fact", "manage_reminders", "search_memory"}:
            kind = "memory"
            title = (
                "Searched synced memory"
                if name == "search_memory"
                else ("Updated synced memory" if name == "remember_fact" else "Updated reminders")
            )
        elif name == "system_control":
            kind = "system"
            act = str(args.get("action") or res.get("action") or "")
            title = f"System control: {act}" if act else "System control"
        return {
            "name": name,
            "kind": kind,
            "title": title,
            "target": target,
            "status": status,
        }

    async def _start_task(self, prompt: str) -> None:
        cleaned = prompt.strip()
        if not cleaned:
            return
        task_id = uuid.uuid4().hex[:12]
        task = self.memory.upsert_task_run(
            task_id=task_id,
            prompt=cleaned,
            status="running",
            summary="",
            actions=[],
        )
        self._active_task = task
        await self._broadcast({"type": "task_event", "event": "started", "task": task})

    async def _record_tool_action(self, payload: dict[str, Any]) -> None:
        if self._active_task is None:
            await self._start_task(self._pending_task_prompt or "Voice / Background command")
        assert self._active_task is not None
        name = str(payload.get("name", "tool"))
        args = payload.get("args") or {}
        phase = str(payload.get("phase", "start"))
        status = "running" if phase == "start" else str(payload.get("status", "ok"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        action_item = self._format_tool_action(name, args, status=status, result=result)

        actions: list[dict[str, Any]] = list(self._active_task.get("actions") or [])
        if phase == "done":
            updated = False
            for idx in range(len(actions) - 1, -1, -1):
                if actions[idx].get("name") == name and actions[idx].get("status") == "running":
                    actions[idx] = action_item
                    updated = True
                    break
            if not updated:
                actions.append(action_item)
        else:
            actions.append(action_item)

        self._active_task = self.memory.upsert_task_run(
            task_id=self._active_task["task_id"],
            prompt=self._active_task["prompt"],
            status="running",
            summary=self._active_task.get("summary", ""),
            actions=actions,
        )
        await self._broadcast({"type": "task_event", "event": "updated", "task": self._active_task})

    @staticmethod
    def _summarize_actions(actions: list[dict[str, Any]], fallback_text: str) -> str:
        done_titles = [
            str(a.get("title", "")).strip()
            for a in actions
            if a.get("title")
        ]
        if done_titles:
            joined = " · ".join(done_titles[:3])
            if len(done_titles) > 3:
                joined += f" (+{len(done_titles) - 3} more)"
            return joined[:220]
        cleaned = " ".join(fallback_text.split())
        return cleaned[:180] + ("..." if len(cleaned) > 180 else "")

    async def _complete_task(self, summary: str) -> None:
        self._pending_task_prompt = ""
        if self._active_task is None:
            return
        actions = list(self._active_task.get("actions") or [])
        if not actions:
            self._active_task = None
            return
        has_error = any(a.get("status") == "error" for a in actions)
        final_status = "error" if has_error else "done"
        concise_summary = self._summarize_actions(actions, summary)
        task = self.memory.upsert_task_run(
            task_id=self._active_task["task_id"],
            prompt=self._active_task["prompt"],
            status=final_status,
            summary=concise_summary,
            actions=actions,
        )
        self._active_task = None
        await self._broadcast({"type": "task_event", "event": "completed", "task": task})

    async def _on_session_event(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "state":
            new_state = str(payload.get("state", self._state))
            if new_state != self._state:
                self.logger.info("state=%s", new_state)
            self._state = new_state
        await self._broadcast(payload)

        ev_type = payload.get("type")
        if ev_type == "transcript" and payload.get("final"):
            role = str(payload.get("role", "")).lower()
            text = str(payload.get("text", "")).strip()
            if role in {"you", "user"} and text:
                self._pending_task_prompt = text
            elif role == "jarvis" and text:
                await self._complete_task(text)
            await self._broadcast(self.build_memory_snapshot())
        elif ev_type == "tool":
            if payload.get("phase") == "done" and payload.get("status") == "rejected":
                self.logger.warning(
                    "Tool rejected (%s): %s",
                    payload.get("name"),
                    (payload.get("result") or {}).get("message", ""),
                )
            await self._record_tool_action(payload)
        elif ev_type == "memory_updated":
            await self._broadcast(self.build_memory_snapshot())

    def build_memory_snapshot(self) -> dict[str, Any]:
        """Build a JSON-serializable snapshot of facts, reminders, recent messages, and tasks."""
        return {
            "type": "memory_snapshot",
            "facts": [asdict(f) for f in self.memory.recall_facts(limit=30)],
            "reminders": [asdict(r) for r in self.memory.list_reminders(include_completed=False)],
            "recent_messages": [asdict(m) for m in self.memory.recent_messages(limit=10)],
            "recent_tasks": self.memory.recent_task_runs(limit=10),
        }

    def build_telemetry(self) -> dict[str, Any]:
        """Collect lightweight Windows system telemetry for the HUD."""
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self.config.workspace_dir))
        battery = psutil.sensors_battery()
        return {
            "type": "telemetry",
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": vm.percent,
            "disk_free_gb": round(disk.free / (1024**3), 1),
            "battery": (
                {
                    "percent": round(battery.percent, 0),
                    "plugged_in": bool(battery.power_plugged),
                }
                if battery is not None
                else None
            ),
            "model": self.config.model,
            "workspace_dir": str(self.config.workspace_dir),
        }

    async def _run_voice_loop(self, mode: str) -> None:
        self._voice_mode = mode
        use_wakeword = mode == "wakeword"
        refresh_audio_devices()
        self._default_endpoints = get_windows_default_endpoints(ensure_mic_unmuted=True)
        loop = asyncio.get_running_loop()
        audio_out = AudioOutputStream(
            sample_rate=self.config.output_sample_rate,
            channels=self.config.audio_channels,
        )

        def _handle_barge_in() -> None:
            audio_out.clear()
            if self._state == "speaking":
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._set_state("listening")))

        audio_in = AudioInputStream(
            sample_rate=self.config.input_sample_rate,
            channels=self.config.audio_channels,
            blocksize=self.config.input_chunk_samples,
            device=None,
            playback_ref=audio_out.playback_ref,
            on_barge_in=_handle_barge_in,
            barge_in_enabled=self.config.barge_in_enabled,
        )
        try:
            mic_queue = audio_in.start(loop)
            audio_out.start()
            self._audio_in = audio_in
            self._audio_out = audio_out
        except Exception as exc:
            self.logger.exception("Audio device startup error")
            await self._broadcast({"type": "error", "message": f"Audio device error ({type(exc).__name__}: {exc})"})
            audio_in.stop()
            audio_out.stop()
            await self._set_state("stopped")
            return

        try:
            if use_wakeword and self._detector is None:
                await self._set_state("loading_wakeword")
                detector = WakeWordDetector(
                    model_name=self.config.wake_word_model,
                    threshold=self.config.wake_threshold,
                    consecutive_frames=self.config.wake_consecutive_frames,
                )
                try:
                    await asyncio.wait_for(asyncio.to_thread(detector.load), timeout=20.0)
                    self._detector = detector
                except Exception as exc:
                    reason = (
                        "Wake word model timed out after 20s"
                        if isinstance(exc, asyncio.TimeoutError)
                        else f"Wake word failed to load ({type(exc).__name__}: {exc})"
                    )
                    await self._broadcast(
                        {
                            "type": "error",
                            "message": f"{reason}. Falling back to direct voice & text mode.",
                        }
                    )
                    use_wakeword = False

            self._outbound_text_queue = asyncio.Queue()
            self._wake_event = asyncio.Event()
            consecutive_api_errors = 0

            async def _drain_cooldown(cooldown_sec: float = 0.8) -> None:
                deadline = loop.time() + cooldown_sec
                while loop.time() < deadline:
                    audio_in.drain()
                    await asyncio.sleep(0.12)
                    if not audio_out.is_playing and (deadline - loop.time()) < (cooldown_sec - 0.45):
                        break
                audio_in.drain()
                reset_fn = getattr(audio_in, "reset_wake_state", None)
                if callable(reset_fn):
                    reset_fn()

            async def _run_live_guarded(*, initial_prompt: str | None = None, wake_greeting: bool = False) -> None:
                nonlocal consecutive_api_errors
                try:
                    reason = await self.session_mgr.run_voice_session(
                        audio_in=audio_in,
                        audio_out=audio_out,
                        mic_queue=mic_queue,
                        idle_timeout_sec=self.config.idle_timeout_sec,
                        initial_prompt=initial_prompt,
                        outbound_text_queue=self._outbound_text_queue,
                        wake_greeting=wake_greeting,
                    )
                    consecutive_api_errors = 0
                    self.logger.info("Voice session ended (reason=%s)", reason)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_api_errors += 1
                    self.session_mgr._resumption_handle = None
                    raw = f"{type(exc).__name__}: {exc}"
                    low = raw.lower()
                    is_quota = "1011" in low or "exhausted" in low or "quota" in low or "429" in low
                    self.logger.warning("Gemini Live error (attempt=%d, quota=%s): %s", consecutive_api_errors, is_quota, raw)
                    friendly = (
                        "Cuota o límite temporal de Gemini Live alcanzado (1011). Reintentando en modo espera..."
                        if is_quota
                        else raw
                    )
                    await self._broadcast({"type": "error", "message": friendly})
                    await asyncio.sleep(min(12.0, 4.0 * consecutive_api_errors))

            if use_wakeword:
                assert self._detector is not None
                while True:
                    await self._set_state("idle_wake")
                    await _drain_cooldown(0.6)
                    self._detector.reset()
                    self._wake_event.clear()

                    while True:
                        if self._wake_event.is_set():
                            self._wake_event.clear()
                            self.logger.info("Wake triggered (source=manual)")
                            await self._broadcast({"type": "wake", "model": "manual", "score": 1.0})
                            audio_in.drain()
                            await _run_live_guarded(wake_greeting=True)
                            break

                        # Allow typed text in the Desktop UI to wake Jarvis too
                        if not self._outbound_text_queue.empty():
                            initial = await self._outbound_text_queue.get()
                            await _run_live_guarded(initial_prompt=initial, wake_greeting=False)
                            break

                        try:
                            frame = await asyncio.wait_for(mic_queue.get(), timeout=0.2)
                        except asyncio.TimeoutError:
                            continue

                        pop_fn = getattr(audio_in, "pop_wake_frame", None)
                        wake_pair = pop_fn() if callable(pop_fn) else None
                        if isinstance(wake_pair, tuple) and len(wake_pair) == 2:
                            wake_frame, speech_active = wake_pair
                        else:
                            wake_frame, speech_active = frame, True

                        detected, score = self._detector.process_frame(
                            wake_frame,
                            speech_active=bool(speech_active),
                        )
                        if detected:
                            self.logger.info(
                                "Wake triggered (model=%s, score=%.3f)",
                                self.config.wake_word_model,
                                score,
                            )
                            await self._broadcast(
                                {"type": "wake", "model": self.config.wake_word_model, "score": score}
                            )
                            self._detector.reset()
                            audio_in.drain()
                            await _run_live_guarded(wake_greeting=True)
                            break
            else:
                while True:
                    try:
                        reason = await self.session_mgr.run_voice_session(
                            audio_in=audio_in,
                            audio_out=audio_out,
                            mic_queue=mic_queue,
                            idle_timeout_sec=None,
                            outbound_text_queue=self._outbound_text_queue,
                        )
                        consecutive_api_errors = 0
                        if reason != "go_away":
                            break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        consecutive_api_errors += 1
                        self.session_mgr._resumption_handle = None
                        raw = f"{type(exc).__name__}: {exc}"
                        low = raw.lower()
                        is_quota = "1011" in low or "exhausted" in low or "quota" in low or "429" in low
                        friendly = (
                            "Cuota o límite temporal de Gemini Live alcanzado (1011). Reconectando..."
                            if is_quota
                            else raw
                        )
                        await self._broadcast({"type": "error", "message": friendly})
                        if consecutive_api_errors >= 3:
                            break
                        await asyncio.sleep(min(12.0, 4.0 * consecutive_api_errors))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._broadcast({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._audio_in = None
            self._audio_out = None
            self._outbound_text_queue = None
            self._wake_event = None
            audio_in.stop()
            audio_out.stop()
            await self._set_state("stopped")

    async def _send_single_text_turn(self, text: str) -> None:
        """Handle a typed message when voice loop is not active."""
        self.memory.log_message("user", text, source="desktop")
        await self._broadcast({"type": "transcript", "role": "user", "text": text, "final": True})
        await self._broadcast(self.build_memory_snapshot())
        await self._set_state("thinking")

        live_cfg = self.session_mgr.build_live_config(text_only=True)
        chunks: list[str] = []
        active_tasks: set[asyncio.Task[None]] = set()

        try:
            async with self.session_mgr._client.aio.live.connect(
                model=self.config.model,
                config=live_cfg,
            ) as session:
                await session.send_realtime_input(text=text)
                while True:
                    received_any = False
                    done_turn = False
                    async for message in session.receive():
                        received_any = True
                        sc = message.server_content
                        if sc:
                            if sc.output_transcription and sc.output_transcription.text:
                                chunks.append(sc.output_transcription.text)
                                await self._broadcast(
                                    {
                                        "type": "transcript",
                                        "role": "jarvis",
                                        "text": "".join(chunks),
                                        "final": False,
                                    }
                                )
                            if sc.model_turn and sc.model_turn.parts:
                                for part in sc.model_turn.parts:
                                    if part.text:
                                        chunks.append(part.text)
                                        await self._broadcast(
                                            {
                                                "type": "transcript",
                                                "role": "jarvis",
                                                "text": "".join(chunks),
                                                "final": False,
                                            }
                                        )

                        if message.tool_call and message.tool_call.function_calls:
                            await self._set_state("tool_running")
                            for call in message.tool_call.function_calls:
                                t = asyncio.create_task(
                                    self.session_mgr._execute_and_respond(session, call)
                                )
                                active_tasks.add(t)
                                t.add_done_callback(active_tasks.discard)

                        if sc and sc.turn_complete and not active_tasks:
                            done_turn = True
                    if not received_any or done_turn:
                        break
        except Exception as exc:
            await self._broadcast({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            full = "".join(chunks).strip()
            if full:
                self.memory.log_message("jarvis", full, source="desktop")
                await self._broadcast(
                    {"type": "transcript", "role": "jarvis", "text": full, "final": True}
                )
                await self._complete_task(full)
            elif self._active_task is not None:
                await self._complete_task("Completed.")
            await self._broadcast(self.build_memory_snapshot())
            await self._set_state("stopped")

    async def _stop_voice(self) -> None:
        if self._voice_task is not None and not self._voice_task.done():
            self._voice_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._voice_task
        self._voice_task = None
        self.session_mgr._resumption_handle = None
        await self._set_state("stopped")

    async def _handle_command(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        action = str(data.get("action", "")).strip()
        if action == "save_api_key":
            api_key = str(data.get("api_key", "")).strip()
            if not api_key:
                await self._broadcast({"type": "error", "message": "API key cannot be empty."})
                return
            self.config.save_api_key(api_key)
            self.session_mgr = JarvisLiveSession(
                config=self.config,
                memory=self.memory,
                registry=self.registry,
                on_event=self._on_session_event,
            )
            await self._broadcast({"type": "needs_api_key", "missing": False})
            await self._broadcast({"type": "api_key_saved", "saved": True})
            if data.get("start_voice", True):
                await self._stop_voice()
                self._voice_task = asyncio.create_task(self._run_voice_loop("wakeword"))
        elif action == "start_voice":
            if not self.config.gemini_api_key:
                await self._broadcast({"type": "needs_api_key", "missing": True})
                return
            mode = str(data.get("mode", "wakeword"))
            await self._stop_voice()
            self._voice_task = asyncio.create_task(self._run_voice_loop(mode))
        elif action == "wake_now":
            if not self.config.gemini_api_key:
                await self._broadcast({"type": "needs_api_key", "missing": True})
                return
            if self._wake_event is not None and self._voice_task and not self._voice_task.done():
                self._wake_event.set()
            else:
                await self._stop_voice()
                self._voice_task = asyncio.create_task(self._run_voice_loop("wakeword"))
                # Trigger wake once loop initializes
                async def _deferred_wake() -> None:
                    for _ in range(50):
                        if self._wake_event is not None:
                            self._wake_event.set()
                            return
                        await asyncio.sleep(0.1)
                asyncio.create_task(_deferred_wake())
        elif action == "stop_voice":
            await self._stop_voice()
        elif action == "restart_voice":
            self.logger.info("Manual voice loop restart requested (mode=%s)", self._voice_mode)
            if not self.config.gemini_api_key:
                await self._broadcast({"type": "needs_api_key", "missing": True})
                return
            await self._stop_voice()
            self._voice_task = asyncio.create_task(self._run_voice_loop(self._voice_mode))
        elif action == "standby":
            if self._voice_task and not self._voice_task.done():
                self.session_mgr.request_standby()
            else:
                if not self.config.gemini_api_key:
                    await self._broadcast({"type": "needs_api_key", "missing": True})
                    return
                await self._stop_voice()
                self._voice_task = asyncio.create_task(self._run_voice_loop("wakeword"))
        elif action == "send_text":
            if not self.config.gemini_api_key:
                await self._broadcast({"type": "needs_api_key", "missing": True})
                return
            text = str(data.get("text", "")).strip()
            if not text:
                return
            self._pending_task_prompt = text
            if self._outbound_text_queue is not None and self._voice_task and not self._voice_task.done():
                await self._outbound_text_queue.put(text)
            else:
                asyncio.create_task(self._send_single_text_turn(text))
        elif action == "new_chat":
            self.memory.clear_chat_history()
            self._active_task = None
            self.session_mgr._resumption_handle = None
            await self._broadcast({"type": "chat_cleared"})
            await self._broadcast(self.build_memory_snapshot())
        elif action == "open_url":
            url = str(data.get("url", "")).strip()
            if url:
                await self._broadcast({"type": "open_viewer", "url": url, "title": url})
        elif action == "add_reminder":
            title = str(data.get("title", "")).strip()
            due_at = str(data.get("due_at", "")).strip() or None
            if title:
                self.memory.add_reminder(title=title, due_at=due_at)
                await self._broadcast(self.build_memory_snapshot())
        elif action == "complete_reminder":
            rid = int(data.get("id", 0))
            if rid > 0:
                self.memory.complete_reminder(rid)
                await self._broadcast(self.build_memory_snapshot())
        elif action == "remember_fact":
            key = str(data.get("key", "")).strip()
            value = str(data.get("value", "")).strip()
            cat = str(data.get("category", "general")).strip() or "general"
            if key and value:
                self.memory.remember_fact(key=key, value=value, category=cat)
                await self._broadcast(self.build_memory_snapshot())
        elif action == "delete_fact":
            key = str(data.get("key", "")).strip()
            if key and self.memory.delete_fact(key):
                await self._broadcast(self.build_memory_snapshot())
        elif action == "get_settings":
            await self._broadcast({"type": "settings_snapshot", "settings": self.config.get_public_settings()})
        elif action == "save_settings":
            raw_settings = data.get("settings")
            if isinstance(raw_settings, dict):
                prev_voice = self.config.voice_name
                prev_silence = self.config.silence_duration_ms
                prev_barge = self.config.barge_in_enabled
                prev_ws = self.config.workspace_dir
                try:
                    updated = self.config.save_settings(raw_settings)
                except ValueError as exc:
                    await self._broadcast({"type": "error", "message": str(exc)})
                    return
                if self.config.workspace_dir != prev_ws:
                    self.registry = build_default_registry(self.config, self.memory)
                    self.session_mgr.registry = self.registry
                    await self._broadcast(self.build_telemetry())
                if self._detector is not None:
                    self._detector.threshold = self.config.wake_threshold
                self.logger.info("Settings updated: %s", updated)
                await self._broadcast({"type": "settings_saved", "settings": updated})
                needs_restart = (
                    updated["voice_name"] != prev_voice
                    or updated["silence_duration_ms"] != prev_silence
                    or updated["barge_in_enabled"] != prev_barge
                    or self.config.workspace_dir != prev_ws
                )
                if needs_restart and self._voice_task and not self._voice_task.done() and self.config.gemini_api_key:
                    self.session_mgr._resumption_handle = None
                    await self._stop_voice()
                    self._voice_task = asyncio.create_task(self._run_voice_loop(self._voice_mode))
        elif action == "set_workspace":
            ws_path = str(data.get("path", "")).strip()
            if not ws_path:
                return
            try:
                self.config.save_workspace_dir(ws_path)
            except ValueError as exc:
                await self._broadcast({"type": "error", "message": str(exc)})
                return
            self.registry = build_default_registry(self.config, self.memory)
            self.session_mgr.registry = self.registry
            self.logger.info("Workspace updated: %s", self.config.workspace_dir)
            await self._broadcast({"type": "settings_saved", "settings": self.config.get_public_settings()})
            await self._broadcast(self.build_telemetry())
            if self._voice_task and not self._voice_task.done() and self.config.gemini_api_key:
                self.session_mgr._resumption_handle = None
                await self._stop_voice()
                self._voice_task = asyncio.create_task(self._run_voice_loop(self._voice_mode))
        elif action == "refresh_memory":
            await self._broadcast(self.build_memory_snapshot())
        elif action == "refresh_devices":
            if not (self._voice_task and not self._voice_task.done()):
                refresh_audio_devices()
            await self._broadcast(
                {
                    "type": "input_devices",
                    "devices": list_input_devices(),
                    "selected": self.config.input_device,
                }
            )
        elif action == "set_input_device":
            device = str(data.get("device", "")).strip()
            self.config.save_input_device(device)
            await self._broadcast(
                {
                    "type": "input_devices",
                    "devices": list_input_devices(),
                    "selected": self.config.input_device,
                }
            )
            if self._voice_task and not self._voice_task.done() and self.config.gemini_api_key:
                await self._stop_voice()
                self._voice_task = asyncio.create_task(self._run_voice_loop(self._voice_mode))
        elif action == "set_telemetry_paused":
            paused = bool(data.get("paused", False))
            was_paused = self._telemetry_paused
            self._telemetry_paused = paused
            if was_paused and not paused:
                await self._broadcast(self.build_telemetry())

    async def _ws_handler(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        try:
            await ws.send(json.dumps({"type": "state", "state": self._state}))
            await ws.send(json.dumps({"type": "needs_api_key", "missing": not bool(self.config.gemini_api_key)}))
            await ws.send(
                json.dumps(
                    {
                        "type": "input_devices",
                        "devices": list_input_devices(),
                        "selected": self.config.input_device,
                    }
                )
            )
            await ws.send(json.dumps(self.build_telemetry(), default=str))
            await ws.send(json.dumps({"type": "settings_snapshot", "settings": self.config.get_public_settings()}))
            await ws.send(json.dumps(self.build_memory_snapshot(), default=str))
            async for message in ws:
                if isinstance(message, str):
                    await self._handle_command(message)
        finally:
            self._clients.discard(ws)

    async def _telemetry_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            if self._telemetry_paused or not self._clients:
                continue
            await self._broadcast(self.build_telemetry())

    async def _check_due_reminders(self) -> None:
        due = self.memory.claim_due_reminders()
        if not due:
            return
        for rem in due:
            self.logger.info("Reminder due #%d: %s (due_at=%s)", rem.id, rem.title, rem.due_at)
            await self._broadcast({"type": "reminder_due", "reminder": asdict(rem)})
        await self._broadcast(self.build_memory_snapshot())

    async def _reminders_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            try:
                await self._check_due_reminders()
            except Exception:
                self.logger.exception("Error checking due reminders")

    async def _mic_level_loop(self) -> None:
        last_sent = -1
        last_out_sent = -1
        ticks = 0
        while True:
            step_sec = (
                0.35
                if self._telemetry_paused
                else (0.25 if self._state in {"idle_wake", "stopped", "offline"} else 0.14)
            )
            await asyncio.sleep(step_sec)
            ticks += 1
            if (
                ticks >= 25
                and self._audio_in is not None
                and self._state in {"idle_wake", "stopped"}
            ):
                ticks = 0
                cur_eps = await asyncio.to_thread(get_windows_default_endpoints, False)
                if cur_eps != ("", "") and self._default_endpoints != ("", "") and cur_eps != self._default_endpoints:
                    self._default_endpoints = await asyncio.to_thread(get_windows_default_endpoints, True)
                    self.logger.info("Default Windows audio endpoints changed -> %s", self._default_endpoints)
                    try:
                        # Close active WASAPI streams BEFORE terminating/re-initializing PortAudio
                        # so Windows audio drivers never spin on orphaned stream handles.
                        self._audio_in.close_stream()
                        if self._audio_out is not None:
                            self._audio_out.close_stream()
                        await asyncio.sleep(0.15)
                        await asyncio.to_thread(refresh_audio_devices)
                        await asyncio.to_thread(self._audio_in.reopen_on_default)
                        if self._audio_out is not None:
                            await asyncio.to_thread(self._audio_out.reopen_on_default)
                    except Exception:
                        self.logger.exception("Failed reopening audio stream after endpoint switch")
            elif ticks >= 25:
                ticks = 0
            if self._telemetry_paused or not self._clients:
                continue
            level = self._audio_in.level_percent if self._audio_in is not None else 0
            out_level = self._audio_out.level_percent if self._audio_out is not None else 0
            if (
                abs(level - last_sent) >= 3
                or abs(out_level - last_out_sent) >= 3
                or (level == 0 and last_sent != 0)
                or (out_level == 0 and last_out_sent != 0)
            ):
                last_sent = level
                last_out_sent = out_level
                await self._broadcast({"type": "mic_level", "level": level, "output_level": out_level})

    async def run(self, auto_start_voice: bool = False) -> None:
        """Start the local WebSocket bridge server."""
        telemetry_task = asyncio.create_task(self._telemetry_loop())
        mic_level_task = asyncio.create_task(self._mic_level_loop())
        reminders_task = asyncio.create_task(self._reminders_loop())
        if auto_start_voice and self.config.gemini_api_key:
            self._voice_task = asyncio.create_task(self._run_voice_loop("wakeword"))
        try:
            async with serve(self._ws_handler, self.host, self.port):
                self.logger.info("Jarvis bridge listening on ws://%s:%d (auto_voice=%s)", self.host, self.port, auto_start_voice)
                print(f"Jarvis bridge listening on ws://{self.host}:{self.port}")
                await asyncio.Future()
        finally:
            telemetry_task.cancel()
            mic_level_task.cancel()
            reminders_task.cancel()
            await self._stop_voice()


def _configure_utf8_stdio() -> None:
    """Prevent Windows cp1252 `UnicodeEncodeError` when printing transcripts/emojis."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Jarvis Desktop Bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--auto-voice", action="store_true")
    args = parser.parse_args()

    config = JarvisConfig.from_env()
    bridge = JarvisBridge(config=config, host=args.host, port=args.port)
    try:
        asyncio.run(bridge.run(auto_start_voice=args.auto_voice))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
