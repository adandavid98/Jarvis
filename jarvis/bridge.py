"""Local WebSocket bridge connecting the Jarvis Electron Desktop UI to the Python core."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import asdict
from typing import Any

import psutil
from websockets.asyncio.server import ServerConnection, serve

from jarvis.audio import AudioInputStream, AudioOutputStream, list_input_devices, refresh_audio_devices
from jarvis.config import JarvisConfig
from jarvis.live_session import JarvisLiveSession
from jarvis.memory import JarvisMemory
from jarvis.tools import build_default_registry
from jarvis.wake_word import WakeWordDetector


class JarvisBridge:
    """Hosts a local WebSocket server for the Desktop UI and manages Jarvis voice/text state."""

    def __init__(self, config: JarvisConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.config = config
        self.host = host
        self.port = port
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
        elif name in {"remember_fact", "manage_reminders"}:
            kind = "memory"
            title = "Updated synced memory" if name == "remember_fact" else "Updated reminders"
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
            self._state = str(payload.get("state", self._state))
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
        }

    async def _run_voice_loop(self, mode: str) -> None:
        self._voice_mode = mode
        use_wakeword = mode == "wakeword"
        refresh_audio_devices()
        await self._broadcast(
            {
                "type": "input_devices",
                "devices": list_input_devices(),
                "selected": self.config.input_device,
            }
        )
        audio_in = AudioInputStream(
            sample_rate=self.config.input_sample_rate,
            channels=self.config.audio_channels,
            blocksize=self.config.input_chunk_samples,
            device=self.config.input_device or None,
        )
        audio_out = AudioOutputStream(
            sample_rate=self.config.output_sample_rate,
            channels=self.config.audio_channels,
        )
        loop = asyncio.get_running_loop()
        try:
            mic_queue = audio_in.start(loop)
            audio_out.start()
            self._audio_in = audio_in
        except Exception as exc:
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

            if use_wakeword:

                while True:
                    await self._set_state("idle_wake")
                    self._detector.reset()
                    self._wake_event.clear()
                    audio_in.drain()

                    while True:
                        if self._wake_event.is_set():
                            self._wake_event.clear()
                            await self._broadcast({"type": "wake", "model": "manual", "score": 1.0})
                            audio_in.drain()
                            await self.session_mgr.run_voice_session(
                                audio_in=audio_in,
                                audio_out=audio_out,
                                mic_queue=mic_queue,
                                idle_timeout_sec=self.config.idle_timeout_sec,
                                outbound_text_queue=self._outbound_text_queue,
                                wake_greeting=True,
                            )
                            break

                        # Allow typed text in the Desktop UI to wake Jarvis too
                        if not self._outbound_text_queue.empty():
                            initial = await self._outbound_text_queue.get()
                            await self.session_mgr.run_voice_session(
                                audio_in=audio_in,
                                audio_out=audio_out,
                                mic_queue=mic_queue,
                                idle_timeout_sec=self.config.idle_timeout_sec,
                                initial_prompt=initial,
                                outbound_text_queue=self._outbound_text_queue,
                            )
                            break

                        try:
                            frame = await asyncio.wait_for(mic_queue.get(), timeout=0.2)
                        except asyncio.TimeoutError:
                            continue

                        detected, score = self._detector.process_frame(frame)
                        if detected:
                            await self._broadcast(
                                {"type": "wake", "model": self.config.wake_word_model, "score": score}
                            )
                            self._detector.reset()
                            audio_in.drain()
                            await self.session_mgr.run_voice_session(
                                audio_in=audio_in,
                                audio_out=audio_out,
                                mic_queue=mic_queue,
                                idle_timeout_sec=self.config.idle_timeout_sec,
                                outbound_text_queue=self._outbound_text_queue,
                                wake_greeting=True,
                            )
                            break
            else:
                while True:
                    reason = await self.session_mgr.run_voice_session(
                        audio_in=audio_in,
                        audio_out=audio_out,
                        mic_queue=mic_queue,
                        idle_timeout_sec=None,
                        outbound_text_queue=self._outbound_text_queue,
                    )
                    if reason != "go_away":
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._broadcast({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self._audio_in = None
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

    async def _mic_level_loop(self) -> None:
        last_sent = -1
        while True:
            await asyncio.sleep(0.15)
            if self._telemetry_paused or not self._clients:
                continue
            level = self._audio_in.level_percent if self._audio_in is not None else 0
            if level != last_sent:
                last_sent = level
                await self._broadcast({"type": "mic_level", "level": level})

    async def run(self, auto_start_voice: bool = False) -> None:
        """Start the local WebSocket bridge server."""
        telemetry_task = asyncio.create_task(self._telemetry_loop())
        mic_level_task = asyncio.create_task(self._mic_level_loop())
        if auto_start_voice and self.config.gemini_api_key:
            self._voice_task = asyncio.create_task(self._run_voice_loop("wakeword"))
        try:
            async with serve(self._ws_handler, self.host, self.port):
                print(f"Jarvis bridge listening on ws://{self.host}:{self.port}")
                await asyncio.Future()
        finally:
            telemetry_task.cancel()
            mic_level_task.cancel()
            await self._stop_voice()


def main() -> int:
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
