"""Unit tests for Jarvis configuration, memory, and automation tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.config import JarvisConfig
from jarvis.live_session import JarvisLiveSession, _extract_interaction_status
from jarvis.memory import JarvisMemory
from jarvis.tools import build_default_registry


@pytest.fixture()
def tmp_config(tmp_path: Path) -> JarvisConfig:
    return JarvisConfig(
        gemini_api_key="test-key",
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
        webhooks={"n8n_test": "https://example.invalid/webhook"},
    )


def test_config_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "abc123")
    monkeypatch.setenv("JARVIS_MODEL", "gemini-3.8-live")
    monkeypatch.setenv("JARVIS_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_WEBHOOKS", json.dumps({"brief": "https://example.com/hook"}))

    cfg = JarvisConfig.from_env()
    assert cfg.gemini_api_key == "abc123"
    assert cfg.model == "gemini-3.8-live"
    assert cfg.workspace_dir == tmp_path.resolve()
    assert cfg.webhooks == {"brief": "https://example.com/hook"}


def test_memory_facts_and_reminders(tmp_path: Path) -> None:
    memory = JarvisMemory(tmp_path / "mem.db")

    memory.remember_fact("Editor", "VS Code", category="preference")
    memory.remember_fact("editor", "Cursor", category="preference")
    facts = memory.recall_facts("cursor")
    assert len(facts) == 1
    assert facts[0].value == "Cursor"

    rem = memory.add_reminder("Check build logs", due_at="17:00")
    active = memory.list_reminders()
    assert len(active) == 1
    assert active[0].id == rem.id

    assert memory.complete_reminder(rem.id) is True
    assert memory.list_reminders() == []

    summary = memory.build_context_summary()
    assert "Cursor" in summary

    # Conversation sync + SYNC.md export
    memory.log_message("user", "Remind me to finish the Electron HUD", source="voice")
    memory.log_message("jarvis", "Got it, saved to your reminders.", source="voice")
    recent = memory.recent_messages(limit=5)
    assert len(recent) == 2
    assert recent[0].role == "user"
    sync_md = (tmp_path / "SYNC.md").read_text(encoding="utf-8")
    assert "Electron HUD" in sync_md
    assert "Cursor" in sync_md


def test_registry_declarations_are_non_blocking(tmp_config: JarvisConfig) -> None:
    memory = JarvisMemory(tmp_config.db_path)
    registry = build_default_registry(tmp_config, memory)

    tool = registry.to_gemini_tool()
    assert tool.function_declarations
    for decl in tool.function_declarations:
        assert str(decl.behavior).endswith("NON_BLOCKING")


def test_desktop_file_and_shell_tools(tmp_config: JarvisConfig) -> None:
    memory = JarvisMemory(tmp_config.db_path)
    registry = build_default_registry(tmp_config, memory)

    async def run() -> None:
        status = await registry.execute("get_system_status", {})
        assert status["status"] == "ok"
        assert "os" in status

        write_res = await registry.execute(
            "write_file",
            {"path": "notes/hello.txt", "content": "hello jarvis"},
        )
        assert write_res["status"] == "ok"

        # Existing file requires overwrite=True
        confirm_res = await registry.execute(
            "write_file",
            {"path": "notes/hello.txt", "content": "updated", "overwrite": False},
        )
        assert confirm_res["status"] == "needs_confirmation"

        # Traversal outside workspace is rejected
        escape_res = await registry.execute(
            "write_file",
            {"path": "../outside.txt", "content": "nope"},
        )
        assert escape_res["status"] == "error"

        read_res = await registry.execute("read_file", {"path": "notes/hello.txt"})
        assert read_res["status"] == "ok"
        assert read_res["content"] == "hello jarvis"

        search_res = await registry.execute("search_code", {"query": "jarvis"})
        assert search_res["status"] == "ok"
        assert any(m["file"].endswith("hello.txt") for m in search_res["matches"])

        shell_ok = await registry.execute(
            "run_shell_command",
            {"command": "Write-Output 'jarvis-ok'"},
        )
        assert shell_ok["status"] == "ok"
        assert "jarvis-ok" in shell_ok["stdout"]

        shell_danger = await registry.execute(
            "run_shell_command",
            {"command": "Remove-Item -Recurse -Force C:\\temp\\fake"},
        )
        assert shell_danger["status"] == "needs_confirmation"

    asyncio.run(run())


def test_productivity_tools_and_webhook(tmp_config: JarvisConfig) -> None:
    memory = JarvisMemory(tmp_config.db_path)
    registry = build_default_registry(tmp_config, memory)

    async def run() -> None:
        rem = await registry.execute(
            "manage_reminders",
            {"action": "add", "title": "Review PR #42"},
        )
        assert rem["status"] == "ok"
        rem_id = rem["reminder"]["id"]

        listed = await registry.execute("manage_reminders", {"action": "list"})
        assert len(listed["reminders"]) == 1

        done = await registry.execute(
            "manage_reminders",
            {"action": "complete", "reminder_id": rem_id},
        )
        assert done["completed"] is True

        bad_hook = await registry.execute("trigger_webhook", {"target": "missing_alias"})
        assert bad_hook["status"] == "error"

        mock_resp = MagicMock(is_success=True, status_code=200, text="accepted")
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_resp

        with patch("jarvis.tools.productivity.httpx.AsyncClient", return_value=mock_client):
            hook_res = await registry.execute(
                "trigger_webhook",
                {"target": "n8n_test", "payload": {"event": "ping"}},
            )
        assert hook_res["status"] == "ok"
        assert hook_res["http_status"] == 200

    asyncio.run(run())


def test_live_config_builds_for_both_38_models(tmp_config: JarvisConfig) -> None:
    memory = JarvisMemory(tmp_config.db_path)
    memory.remember_fact("os", "Windows 11", category="system")
    registry = build_default_registry(tmp_config, memory)

    # 1. Extended thinking model includes thinking_config
    ext_config = JarvisConfig(
        gemini_api_key="test-key",
        model="gemini-3.8-live-extended-thinking",
        db_path=tmp_config.db_path,
        workspace_dir=tmp_config.workspace_dir,
    )
    ext_session = JarvisLiveSession(ext_config, memory, registry)
    audio_cfg = ext_session.build_live_config(text_only=False)
    assert audio_cfg.thinking_config is not None
    assert audio_cfg.input_audio_transcription is not None
    assert audio_cfg.context_window_compression is not None
    assert audio_cfg.session_resumption is not None

    # 2. Standard gemini-3.8-live omits thinking_config
    std_config = JarvisConfig(
        gemini_api_key="test-key",
        model="gemini-3.8-live",
        db_path=tmp_config.db_path,
        workspace_dir=tmp_config.workspace_dir,
    )
    std_session = JarvisLiveSession(std_config, memory, registry)
    std_audio_cfg = std_session.build_live_config(text_only=False)
    assert std_audio_cfg.thinking_config is None
    text_cfg = std_session.build_live_config(text_only=True)
    assert text_cfg.speech_config is None
    assert text_cfg.output_audio_transcription is not None

    # 3. _extract_interaction_status reads server_content.interaction_status
    from google.genai import types

    msg = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            interaction_status=types.InteractionStatus.IDLE
        )
    )
    assert _extract_interaction_status(msg) == "IDLE"


def test_task_runs_limit_and_clear_chat(tmp_config: JarvisConfig) -> None:
    memory = JarvisMemory(tmp_config.db_path)
    memory.remember_fact("editor", "VS Code")
    memory.add_reminder("Keep this reminder")

    for idx in range(12):
        memory.upsert_task_run(
            task_id=f"task-{idx}",
            prompt=f"Prompt {idx}",
            status="done",
            summary=f"Summary {idx}",
            actions=[{"name": "run_shell_command", "kind": "command", "title": f"Cmd {idx}", "status": "ok"}],
        )
        memory.log_message("user", f"Msg {idx}", source="desktop")

    recent_tasks = memory.recent_task_runs(limit=10)
    assert len(recent_tasks) == 10
    assert recent_tasks[0]["task_id"] == "task-2"
    assert recent_tasks[-1]["task_id"] == "task-11"

    memory.clear_chat_history()
    assert memory.recent_task_runs(limit=10) == []
    assert memory.recent_messages(limit=10) == []
    assert len(memory.recall_facts()) == 1
    assert len(memory.list_reminders()) == 1


def test_web_search_and_embedded_viewer_url(tmp_config: JarvisConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_EMBEDDED_VIEWER", "1")
    memory = JarvisMemory(tmp_config.db_path)
    registry = build_default_registry(tmp_config, memory)

    async def run() -> None:
        search_res = await registry.execute("web_search", {"query": "electron webcontentsview"})
        assert search_res["status"] == "ok"
        assert "duckduckgo.com/?q=electron+webcontentsview" in search_res["viewer_url"]

        open_res = await registry.execute("open_application_or_url", {"target": "https://github.com/adandavid98/Jarvis"})
        assert open_res["status"] == "ok"
        assert open_res["viewer_url"] == "https://github.com/adandavid98/Jarvis"

        standby_res = await registry.execute("enter_standby", {})
        assert standby_res["status"] == "ok"
        assert standby_res["standby"] is True

    asyncio.run(run())
    assert JarvisConfig().idle_timeout_sec == 15.0


def test_bridge_save_api_key_and_wakeword_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.bridge import JarvisBridge

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    cfg = JarvisConfig(
        gemini_api_key="",
        data_dir=tmp_path,
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
    )
    bridge = JarvisBridge(config=cfg)
    events: list[dict[str, object]] = []

    async def fake_broadcast(payload: dict[str, object]) -> None:
        events.append(payload)

    bridge._broadcast = fake_broadcast  # type: ignore[method-assign]

    async def run() -> None:
        await bridge._handle_command(json.dumps({"action": "save_api_key", "api_key": "new-key-123", "start_voice": False}))
        assert bridge.config.gemini_api_key == "new-key-123"
        assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == "GEMINI_API_KEY=new-key-123"
        assert {"type": "needs_api_key", "missing": False} in events
        assert {"type": "api_key_saved", "saved": True} in events

        # Verify wakeword load failure falls back to direct mode instead of hanging
        fake_in = MagicMock()
        fake_in.start.return_value = asyncio.Queue()
        fake_out = MagicMock()
        bridge.session_mgr.run_voice_session = AsyncMock(return_value="closed")  # type: ignore[method-assign]

        with (
            patch("jarvis.bridge.AudioInputStream", return_value=fake_in),
            patch("jarvis.bridge.AudioOutputStream", return_value=fake_out),
            patch("jarvis.bridge.WakeWordDetector") as mock_det_cls,
        ):
            mock_det_cls.return_value.load.side_effect = RuntimeError("onnx failed")
            await bridge._run_voice_loop("wakeword")

        assert any(
            ev.get("type") == "error" and "Falling back to direct voice" in str(ev.get("message", ""))
            for ev in events
        )
        bridge.session_mgr.run_voice_session.assert_awaited_once()

    asyncio.run(run())


def test_create_document_pdf_docx_and_full_pc_access(tmp_path: Path) -> None:
    outside_dir = tmp_path / "downloads_sim"
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    cfg = JarvisConfig(
        gemini_api_key="test-key",
        db_path=tmp_path / "jarvis.db",
        workspace_dir=ws_dir,
        auto_approve_dangerous=True,
    )
    memory = JarvisMemory(cfg.db_path)
    registry = build_default_registry(cfg, memory)

    async def run() -> None:
        pdf_path = outside_dir / "informe.pdf"
        pdf_res = await registry.execute(
            "create_document",
            {"path": str(pdf_path), "title": "Informe Jarvis", "content": "Contenido en español con acentos: áéíóúñ."},
        )
        assert pdf_res["status"] == "ok"
        assert pdf_path.exists() and pdf_path.stat().st_size > 100

        docx_path = outside_dir / "nota.docx"
        docx_res = await registry.execute(
            "create_document",
            {"path": str(docx_path), "title": "Nota", "content": "Línea 1\nLínea 2"},
        )
        assert docx_res["status"] == "ok"
        assert docx_path.exists() and docx_path.stat().st_size > 100

        txt_path = outside_dir / "codigo.py"
        await registry.execute("write_file", {"path": str(txt_path), "content": "x = 1\n"})
        edit_res = await registry.execute(
            "edit_file",
            {"path": str(txt_path), "target_text": "x = 1", "replacement": "x = 42"},
        )
        assert edit_res["status"] == "ok"
        assert txt_path.read_text(encoding="utf-8") == "x = 42\n"

    asyncio.run(run())


def test_bridge_tasks_only_track_tool_actions(tmp_path: Path) -> None:
    from jarvis.bridge import JarvisBridge

    cfg = JarvisConfig(
        gemini_api_key="test-key",
        data_dir=tmp_path,
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
    )
    bridge = JarvisBridge(config=cfg)
    events: list[dict[str, object]] = []

    async def fake_broadcast(payload: dict[str, object]) -> None:
        events.append(payload)

    bridge._broadcast = fake_broadcast  # type: ignore[method-assign]

    async def run() -> None:
        # Plain chat without tools should not create a task_run
        await bridge._on_session_event({"type": "transcript", "role": "user", "text": "Hola Jarvis", "final": True})
        await bridge._on_session_event({"type": "transcript", "role": "jarvis", "text": "Hola, ¿en qué ayudo?", "final": True})
        assert bridge.memory.recent_task_runs(limit=10) == []

        # Chat with a tool call should create a task_run with concise action summary
        await bridge._on_session_event({"type": "transcript", "role": "user", "text": "Crea un pdf", "final": True})
        await bridge._on_session_event({
            "type": "tool",
            "phase": "done",
            "name": "create_document",
            "status": "ok",
            "args": {"path": "C:/Users/demo/Downloads/doc.pdf"},
            "result": {"status": "ok", "path": "C:/Users/demo/Downloads/doc.pdf"},
        })
        await bridge._on_session_event({"type": "transcript", "role": "jarvis", "text": "Listo.", "final": True})
        tasks = bridge.memory.recent_task_runs(limit=10)
        assert len(tasks) == 1
        assert "Created document: C:/Users/demo/Downloads/doc.pdf" in tasks[0]["summary"]

    asyncio.run(run())


def test_audio_agc_and_wake_threshold() -> None:
    import numpy as np
    from jarvis.audio import AudioInputStream
    from jarvis.wake_word import WakeWordDetector

    cfg = JarvisConfig.from_env()
    assert cfg.wake_threshold == 0.28
    assert cfg.wake_consecutive_frames == 2
    assert cfg.mute_mic_while_speaking is False
    assert cfg.silence_duration_ms == 1100
    assert cfg.prefix_padding_ms == 300
    assert cfg.barge_in_enabled is True

    det = WakeWordDetector()
    assert det.threshold == 0.28
    assert det.consecutive_frames == 2

    stream = AudioInputStream(sample_rate=16000, channels=1, blocksize=1280)
    t = np.linspace(0.0, 0.08, 1280, endpoint=False)
    # Simulate quiet 400Hz laptop speech (peak ~600) plus 30Hz fan rumble
    quiet_speech = (600.0 * np.sin(2 * np.pi * 400 * t) + 300.0 * np.sin(2 * np.pi * 30 * t)).astype(np.int16)
    boosted = stream._preprocess_and_agc(quiet_speech)
    assert boosted.dtype == np.int16
    assert len(boosted) == 1280
    # AGC should amplify speech well above the raw 600 peak
    assert int(np.max(np.abs(boosted))) > 2000
    assert stream.level_percent >= 20


def test_wake_word_multiframe_and_noise_gate() -> None:
    import numpy as np
    from jarvis.wake_word import WakeWordDetector

    class _FakeModel:
        def __init__(self, scores: list[float]) -> None:
            self.scores = list(scores)
            self.frames: list[np.ndarray] = []

        def predict(self, frame: np.ndarray) -> dict[str, float]:
            self.frames.append(frame.copy())
            val = self.scores.pop(0) if self.scores else 0.0
            return {"hey_jarvis": val}

        def reset(self) -> None:
            pass

    t = np.linspace(0.0, 0.08, 1280, endpoint=False)
    loud_frame = (1500.0 * np.sin(2 * np.pi * 400 * t)).astype(np.int16)

    det = WakeWordDetector(threshold=0.28, consecutive_frames=2, instant_threshold=0.60, min_voice_rms=160.0)
    det._model = _FakeModel([0.45, 0.10, 0.35, 0.40])

    # Single spike < instant_threshold must NOT trigger
    hit1, s1 = det.process_frame(loud_frame, speech_active=True)
    assert hit1 is False and abs(s1 - 0.45) < 1e-6

    # Drop resets streak
    hit2, _ = det.process_frame(loud_frame, speech_active=True)
    assert hit2 is False

    # Two consecutive frames >= 0.28 MUST trigger
    hit3, _ = det.process_frame(loud_frame, speech_active=True)
    assert hit3 is False
    hit4, s4 = det.process_frame(loud_frame, speech_active=True)
    assert hit4 is True and abs(s4 - 0.40) < 1e-6

    # Non-speech ambient noise flushes zeros and resets streak
    fake_silent = _FakeModel([0.55])
    det2 = WakeWordDetector(threshold=0.28, consecutive_frames=2, instant_threshold=0.60, min_voice_rms=160.0)
    det2._model = fake_silent
    hit_sil, score_sil = det2.process_frame(loud_frame, speech_active=False)
    assert hit_sil is False and score_sil == 0.0
    assert int(np.max(np.abs(fake_silent.frames[0]))) == 0


def test_echo_canceller_and_barge_in_and_preroll() -> None:
    import numpy as np
    from jarvis.audio import AudioInputStream, EchoCanceller, PlaybackReferenceBuffer

    ref_buf = PlaybackReferenceBuffer(target_rate=16000, max_seconds=3.0)
    t24 = np.linspace(0.0, 0.24, int(24000 * 0.24), endpoint=False)
    speaker_24k = (8000.0 * np.sin(2 * np.pi * 320 * t24) + 4000.0 * np.sin(2 * np.pi * 640 * t24)).astype(np.int16)
    ref_buf.push_24k(speaker_24k, source_rate=24000)

    hist, active = ref_buf.snapshot(max_samples=6400)
    assert active is True and len(hist) > 1280

    # Simulate acoustic path: 400-sample delay + 0.5x attenuation
    delay = 400
    mic_echo = (0.5 * hist[-(1280 + delay):-delay]).astype(np.int16)
    aec = EchoCanceller(num_taps=25, max_lag_samples=2880)
    cleaned_echo, dt_echo = aec.process(mic_echo, hist, noise_rms=60.0)
    assert dt_echo is False
    assert float(np.sqrt(np.mean(cleaned_echo.astype(np.float32) ** 2))) < float(np.sqrt(np.mean(mic_echo.astype(np.float32) ** 2))) * 0.5

    # Now overlay strong near-end speech (different frequency & uncorrelated) -> double talk!
    t16 = np.linspace(0.0, 0.08, 1280, endpoint=False)
    near_speech = (4500.0 * np.sin(2 * np.pi * 950 * t16)).astype(np.int16)
    mic_double = np.clip(mic_echo.astype(np.int32) + near_speech.astype(np.int32), -32768, 32767).astype(np.int16)
    _, dt_speech = aec.process(mic_double, hist, noise_rms=60.0)
    assert dt_speech is True

    # Test AudioInputStream pre-roll capture on onset & barge-in callback
    barge_calls = 0

    def _on_barge() -> None:
        nonlocal barge_calls
        barge_calls += 1

    stream = AudioInputStream(
        sample_rate=16000,
        channels=1,
        blocksize=1280,
        playback_ref=ref_buf,
        on_barge_in=_on_barge,
        barge_in_enabled=True,
    )
    silence = np.zeros(1280, dtype=np.int16)
    stream._process_dual_branch(silence)
    stream._process_dual_branch(silence)
    assert stream.is_speech_active is False

    # Feed near-end voice frames while playback is active -> barge-in + pre-roll
    stream._process_dual_branch(mic_double)
    stream._process_dual_branch(mic_double)
    assert stream.is_speech_active is True
    assert barge_calls >= 1
    assert len(stream._pending_preroll) >= 1


def test_live_config_vad_and_quota_recovery(tmp_path: Path) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    import numpy as np
    from google.genai import types
    from jarvis.config import JarvisConfig
    from jarvis.live_session import JarvisLiveSession
    from jarvis.memory import JarvisMemory
    from jarvis.tools import build_default_registry

    cfg = JarvisConfig(
        gemini_api_key="dummy-key",
        db_path=tmp_path / "m.db",
        data_dir=tmp_path,
        silence_duration_ms=1100,
        prefix_padding_ms=300,
        barge_in_enabled=True,
    )
    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)
    session = JarvisLiveSession(cfg, mem, reg)
    live_cfg = session.build_live_config(text_only=False)
    ric = live_cfg.realtime_input_config
    assert ric is not None
    assert ric.automatic_activity_detection.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW
    assert ric.automatic_activity_detection.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_HIGH
    assert ric.automatic_activity_detection.silence_duration_ms == 1100
    assert ric.automatic_activity_detection.prefix_padding_ms == 300
    assert ric.activity_handling == types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    assert ric.turn_coverage == types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY

    sent_calls: list[dict[str, object]] = []

    class _FakeGeminiSession:
        async def send_realtime_input(self, **kwargs: object) -> None:
            sent_calls.append(kwargs)

        async def send_client_content(self, **kwargs: object) -> None:
            pass

        async def receive(self):
            while True:
                await asyncio.sleep(0.05)
                if False:
                    yield None

    class _FakeLive:
        @asynccontextmanager
        async def connect(self, **kwargs: object):
            yield _FakeGeminiSession()

    class _FakeAio:
        def __init__(self) -> None:
            self.live = _FakeLive()

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = _FakeAio()

    session._client = _FakeClient()  # type: ignore[assignment]

    # Create cached wake greeting PCM so wake_greeting replays locally without prompt
    greeting_pcm_path = tmp_path / "wake_greeting_24k.pcm"
    greeting_pcm_path.write_bytes(b"\x01\x00" * 5000)

    class _FakeAudioOut:
        def __init__(self) -> None:
            self.enqueued: list[bytes] = []
            self.is_playing = False

        def enqueue(self, pcm: bytes) -> None:
            self.enqueued.append(pcm)

        def clear(self) -> None:
            pass

    class _FakeAudioIn:
        def __init__(self) -> None:
            self.is_speech_active = True

        def drain(self) -> None:
            pass

    async def _exercise_session() -> str:
        mic_q: asyncio.Queue[np.ndarray] = asyncio.Queue()
        fake_in = _FakeAudioIn()
        fake_out = _FakeAudioOut()

        async def _feed() -> None:
            fake_in.is_speech_active = True
            await mic_q.put(np.ones(1280, dtype=np.int16) * 200)
            await asyncio.sleep(0.03)
            fake_in.is_speech_active = False
            await mic_q.put(np.zeros(1280, dtype=np.int16))
            await asyncio.sleep(0.03)
            session.request_standby()

        feeder = asyncio.create_task(_feed())
        reason = await session.run_voice_session(
            fake_in,  # type: ignore[arg-type]
            fake_out,  # type: ignore[arg-type]
            mic_q,
            idle_timeout_sec=5.0,
            wake_greeting=True,
        )
        await feeder
        assert len(fake_out.enqueued) == 1
        return reason

    reason = asyncio.run(_exercise_session())
    assert reason == "standby"
    assert any("audio" in c for c in sent_calls)
    assert any(c.get("audio_stream_end") is True for c in sent_calls)
    assert not any("text" in c for c in sent_calls)





