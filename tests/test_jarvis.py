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
    assert cfg.wake_threshold == 0.50
    assert cfg.wake_consecutive_frames == 2
    assert cfg.mute_mic_while_speaking is False
    assert cfg.silence_duration_ms == 2200
    assert cfg.prefix_padding_ms == 600
    assert cfg.barge_in_enabled is True

    det = WakeWordDetector()
    assert det.threshold == 0.50
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

    # Decoupled dual-branch check: distant low-amplitude voice activates wake branch
    # without leaking distant room chatter into Gemini Live voice branch
    far_stream = AudioInputStream(sample_rate=16000, channels=1, blocksize=1280)
    quiet_room = (25.0 * np.sin(2 * np.pi * 400 * t)).astype(np.int16)
    for _ in range(7):
        far_stream._process_dual_branch(quiet_room)
    distant_voice = (100.0 * np.sin(2 * np.pi * 400 * t)).astype(np.int16)
    v_out, w_out, v_active = far_stream._process_dual_branch(distant_voice)
    assert v_active is False and far_stream.is_speech_active is False
    assert int(np.max(np.abs(v_out))) == 0
    assert far_stream.is_wake_active is True
    assert int(np.max(np.abs(w_out))) > 0


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
    assert ric.automatic_activity_detection.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_LOW
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
    greeting_pcm_path = tmp_path / f"wake_greeting_24k_{cfg.voice_name.lower()}.pcm"
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


def test_wake_greeting_recording_without_cache(tmp_path: Path) -> None:
    """Ensure first uncached wake greeting records audio without UnboundLocalError and saves PCM cache."""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import numpy as np
    from jarvis.config import JarvisConfig
    from jarvis.live_session import JarvisLiveSession
    from jarvis.memory import JarvisMemory
    from jarvis.tools import build_default_registry

    cfg = JarvisConfig(
        gemini_api_key="dummy-key",
        db_path=tmp_path / "m2.db",
        data_dir=tmp_path,
    )
    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)
    session = JarvisLiveSession(cfg, mem, reg)

    greeting_bytes = b"\x10\x00" * 5000

    class _FakeGeminiSession:
        async def send_realtime_input(self, **kwargs: object) -> None:
            pass

        async def send_client_content(self, **kwargs: object) -> None:
            pass

        async def receive(self):
            yield SimpleNamespace(
                server_content=SimpleNamespace(
                    interrupted=False,
                    model_turn=SimpleNamespace(
                        parts=[SimpleNamespace(inline_data=SimpleNamespace(data=greeting_bytes))]
                    ),
                    input_transcription=None,
                    output_transcription=SimpleNamespace(text="Hola, ¿cómo te ayudo?"),
                    turn_complete=True,
                ),
                tool_call=None,
                go_away=None,
                session_resumption_update=None,
            )
            await asyncio.sleep(0.05)
            session.request_standby()
            while True:
                await asyncio.sleep(0.05)

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

    class _FakeAudioOut:
        def __init__(self) -> None:
            self.enqueued: list[bytes] = []
            self.is_playing = False

        def enqueue(self, pcm: bytes) -> None:
            self.enqueued.append(pcm)

        def clear(self) -> None:
            pass

    class _FakeAudioIn:
        is_speech_active = False

        def drain(self) -> None:
            pass

    reason = asyncio.run(
        session.run_voice_session(
            _FakeAudioIn(),  # type: ignore[arg-type]
            _FakeAudioOut(),  # type: ignore[arg-type]
            asyncio.Queue(),
            idle_timeout_sec=5.0,
            wake_greeting=True,
        )
    )
    assert reason == "standby"
    cache_path = tmp_path / f"wake_greeting_24k_{cfg.voice_name.lower()}.pcm"
    assert cache_path.exists()
    assert cache_path.read_bytes() == greeting_bytes


def test_bridge_rotating_logger_and_restart_voice(tmp_path: Path) -> None:
    from jarvis.bridge import JarvisBridge, _setup_bridge_logger

    logger = _setup_bridge_logger(tmp_path)
    log_path = tmp_path / "logs" / "bridge.log"
    assert log_path.parent.is_dir()

    logger.info("bridge-log-check")
    for h in logger.handlers:
        h.flush()
    assert log_path.exists()
    assert "bridge-log-check" in log_path.read_text(encoding="utf-8")

    cfg = JarvisConfig(
        gemini_api_key="test-key",
        data_dir=tmp_path,
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
    )
    bridge = JarvisBridge(config=cfg)
    bridge._voice_mode = "direct"

    started_modes: list[str] = []

    async def fake_run_voice(mode: str) -> None:
        started_modes.append(mode)

    bridge._run_voice_loop = fake_run_voice  # type: ignore[method-assign]

    async def run() -> None:
        await bridge._handle_command(json.dumps({"action": "restart_voice"}))
        assert bridge._voice_task is not None
        await bridge._voice_task

    asyncio.run(run())
    assert started_modes == ["direct"]

    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_config_save_settings_and_memory_delete_due_reminders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta
    from jarvis.memory import normalize_due_at

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    cfg = JarvisConfig(
        gemini_api_key="k",
        data_dir=tmp_path,
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
    )
    saved = cfg.save_settings(
        {
            "voice_name": "Charon",
            "wake_threshold": 0.99,
            "silence_duration_ms": 200,
            "idle_timeout_sec": 30,
            "barge_in_enabled": False,
            "auto_approve_dangerous": True,
        }
    )
    assert saved["voice_name"] == "Charon"
    assert saved["wake_threshold"] == 0.90
    assert saved["silence_duration_ms"] == 500
    assert saved["idle_timeout_sec"] == 30.0
    assert saved["barge_in_enabled"] is False
    assert saved["auto_approve_dangerous"] is True
    env_txt = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "JARVIS_VOICE_NAME=Charon" in env_txt
    assert "JARVIS_WAKE_THRESHOLD=0.90" in env_txt

    memory = JarvisMemory(tmp_path / "jarvis.db")
    memory.remember_fact("editor", "VS Code", "work")
    assert len(memory.recall_facts("editor")) == 1
    assert memory.delete_fact("editor") is True
    assert memory.recall_facts("editor") == []

    ref = datetime(2026, 11, 15, 10, 0, 0)
    assert normalize_due_at("+15m", now=ref) == "2026-11-15 10:15:00"
    assert normalize_due_at("14:30", now=ref) == "2026-11-15 14:30:00"

    past_due = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    future_due = (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    r_due = memory.add_reminder("Llamar al cliente", due_at=past_due)
    memory.add_reminder("Reunion tarde", due_at=future_due)

    claimed = memory.claim_due_reminders()
    assert [r.id for r in claimed] == [r_due.id]
    assert memory.claim_due_reminders() == []


def test_system_control_and_bridge_settings_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.bridge import JarvisBridge

    cfg = JarvisConfig(
        gemini_api_key="k",
        data_dir=tmp_path,
        db_path=tmp_path / "jarvis.db",
        workspace_dir=tmp_path,
        auto_approve_dangerous=False,
    )
    memory = JarvisMemory(cfg.db_path)
    registry = build_default_registry(cfg, memory)

    fake_user32 = MagicMock()
    monkeypatch.setattr("jarvis.tools.desktop.os.name", "nt")
    monkeypatch.setattr("jarvis.tools.desktop.ctypes", MagicMock(windll=MagicMock(user32=fake_user32)), raising=False)

    async def run_tools() -> None:
        res_vol = await registry.execute("system_control", {"action": "volume_up", "value": 3})
        assert res_vol["status"] == "ok"
        assert fake_user32.keybd_event.call_count == 6

        res_lock = await registry.execute("system_control", {"action": "lock_screen"})
        assert res_lock["status"] == "needs_confirmation"

        with patch("jarvis.tools.desktop.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="hola portapapeles", stderr="")
            res_clip = await registry.execute("system_control", {"action": "clipboard_get"})
            assert res_clip["status"] == "ok"
            assert res_clip["text"] == "hola portapapeles"

    asyncio.run(run_tools())

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    bridge = JarvisBridge(config=cfg)
    events: list[dict[str, object]] = []

    async def fake_broadcast(payload: dict[str, object]) -> None:
        events.append(payload)

    bridge._broadcast = fake_broadcast  # type: ignore[method-assign]
    bridge.memory.remember_fact("os", "Windows", "system")

    async def run_bridge() -> None:
        await bridge._handle_command(json.dumps({"action": "delete_fact", "key": "os"}))
        assert bridge.memory.recall_facts("os") == []
        await bridge._handle_command(
            json.dumps({"action": "save_settings", "settings": {"voice_name": "Puck", "wake_threshold": 0.42}})
        )
        assert bridge.config.voice_name == "Puck"
        assert bridge.config.wake_threshold == 0.42
        assert any(ev.get("type") == "settings_saved" for ev in events)

    asyncio.run(run_bridge())


def test_fts5_memory_search_and_web_search_snippets(tmp_path: Path) -> None:
    db_path = tmp_path / "fts_test.db"
    memory = JarvisMemory(db_path)
    memory.remember_fact("servidor_produccion", "IP 10.20.30.40 puerto 8443", "work")
    memory.remember_fact("cafe", "Americano sin azúcar", "preference")
    memory.log_message("user", "Anota que la reunión de arquitectura es el jueves", source="voice")
    memory.log_message("jarvis", "Listo, guardado sobre la reunión de arquitectura.", source="voice")

    res = memory.search_memory("arquitectura produccion")
    assert any(f["key"] == "servidor_produccion" for f in res["facts"])
    assert any("arquitectura" in c["snippet"].lower() for c in res["conversations"])

    # Verify diacritic-insensitive lookup via FTS5 ("azucar" finds "azúcar")
    sweet = memory.recall_facts(query="azucar")
    assert any(f.key == "cafe" for f in sweet)

    cfg = JarvisConfig(
        gemini_api_key="k",
        data_dir=tmp_path,
        db_path=db_path,
        workspace_dir=tmp_path,
    )
    registry = build_default_registry(cfg, memory)

    fake_html = """
    <html><body>
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Python 3.13 Release</a>
      <a class="result__snippet">Python 3.13 introduces an improved interactive interpreter and free-threaded mode.</a>
      <a class="result__a" href="https://example.com/b">Docs Overview</a>
      <a class="result__snippet">Official documentation &amp; changelog highlights.</a>
    </body></html>
    """

    class _FakeResp:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

        def json(self) -> dict[str, object]:
            return {}

    class _FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, params: dict[str, object] | None = None) -> _FakeResp:
            if "html.duckduckgo.com" in url:
                return _FakeResp(200, fake_html)
            return _FakeResp(404, "")

    async def run() -> None:
        mem_tool = await registry.execute("search_memory", {"query": "servidor"})
        assert mem_tool["status"] == "ok"
        assert mem_tool["facts"][0]["key"] == "servidor_produccion"

        with patch("jarvis.tools.desktop.httpx.AsyncClient", _FakeAsyncClient):
            ws_res = await registry.execute("web_search", {"query": "python 3.13"})
            assert ws_res["status"] == "ok"
            assert len(ws_res["snippets"]) == 2
            assert ws_res["snippets"][0]["url"] == "https://example.com/a"
            assert "free-threaded mode" in ws_res["snippets"][0]["snippet"]

    asyncio.run(run())


def test_configure_utf8_stdio_handles_cp1252_unencodable() -> None:
    import io
    from jarvis.bridge import _configure_utf8_stdio

    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    with patch("jarvis.bridge.sys.stdout", wrapper):
        _configure_utf8_stdio()
        print("Jarvis listo ⚡ 🤖 ✓")
        wrapper.flush()
    decoded = raw.getvalue().decode("utf-8")
    assert "Jarvis listo" in decoded


def test_standby_guard_and_idle_watchdog_hard_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import numpy as np
    from jarvis.audio import AudioInputStream
    from jarvis.config import JarvisConfig
    from jarvis.live_session import JarvisLiveSession
    from jarvis.memory import JarvisMemory
    from jarvis.tools import build_default_registry

    # 1. Verify AudioInputStream reset_wake_state clears wake envelope & hangover
    stream = AudioInputStream(sample_rate=16000, channels=1, blocksize=1280)
    stream._wake_env_rms = 999.0
    stream._hangover = 7
    stream.is_speech_active = True
    stream.reset_wake_state()
    assert stream._wake_env_rms == 250.0
    assert stream._hangover == 0
    assert stream.is_speech_active is False

    # 2. Verify enter_standby is rejected unless user explicitly asked for standby/sleep/goodbye
    cfg = JarvisConfig(gemini_api_key="dummy-key", db_path=tmp_path / "m.db", data_dir=tmp_path)
    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)
    session_mgr = JarvisLiveSession(cfg, mem, reg)
    standby_ev = asyncio.Event()
    session_mgr._standby_event = standby_ev

    sent_tool_responses: list[object] = []

    class _MockToolSession:
        async def send_tool_response(self, *, function_responses):
            sent_tool_responses.extend(function_responses)

    async def exercise_standby_guard() -> None:
        mock_sess = _MockToolSession()
        call = SimpleNamespace(id="c1", name="enter_standby", args={})

        session_mgr._record_user_utterance("editarla entonces si yo por ejemplo la edito")
        await session_mgr._execute_and_respond(mock_sess, call)
        assert not standby_ev.is_set()
        assert sent_tool_responses[-1].response["status"] == "rejected"

        session_mgr._record_user_utterance("Jarvis, eso es todo, descansa")
        await session_mgr._execute_and_respond(mock_sess, call)
        assert standby_ev.is_set()
        assert sent_tool_responses[-1].response["status"] == "ok"

    asyncio.run(exercise_standby_guard())

    # 3. Verify noisy speech_active blips cannot keep session stuck past idle_timeout_sec + 5.0
    class _SilentGeminiSession:
        async def send_realtime_input(self, **kwargs: object) -> None:
            pass

        async def send_client_content(self, **kwargs: object) -> None:
            pass

        async def receive(self):
            while True:
                await asyncio.sleep(0.05)
                if False:
                    yield None

    class _FakeClient:
        def __init__(self) -> None:
            @asynccontextmanager
            async def _connect(**kwargs: object):
                yield _SilentGeminiSession()

            self.aio = SimpleNamespace(live=SimpleNamespace(connect=_connect))

    session_mgr2 = JarvisLiveSession(cfg, mem, reg)
    session_mgr2._client = _FakeClient()  # type: ignore[assignment]

    fake_clock = [1000.0]
    monkeypatch.setattr("jarvis.live_session._monotonic", lambda: fake_clock[0])

    class _NoisyAudioIn:
        is_speech_active = True

        def drain(self) -> None:
            pass

    class _IdleAudioOut:
        is_playing = False

        def enqueue(self, pcm: bytes) -> None:
            pass

        def clear(self) -> None:
            pass

    async def exercise_idle_cap() -> str:
        mic_q: asyncio.Queue[np.ndarray] = asyncio.Queue()

        async def _noise_loop() -> None:
            for step in range(12):
                fake_clock[0] += 1.0
                await mic_q.put(np.ones(1280, dtype=np.int16) * 50)
                await asyncio.sleep(0.05)

        feeder = asyncio.create_task(_noise_loop())
        reason = await session_mgr2.run_voice_session(
            _NoisyAudioIn(),  # type: ignore[arg-type]
            _IdleAudioOut(),  # type: ignore[arg-type]
            mic_q,
            idle_timeout_sec=2.0,
        )
        feeder.cancel()
        return reason

    assert asyncio.run(exercise_idle_cap()) == "idle_timeout"


def test_workspace_switch_academic_docs_and_research_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()

    cfg = JarvisConfig(
        gemini_api_key="test-key",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "jarvis.db",
        workspace_dir=ws_a,
        auto_approve_dangerous=True,
    )
    updated = cfg.save_settings({"workspace_dir": str(ws_b)})
    assert Path(updated["workspace_dir"]) == ws_b.resolve()
    assert cfg.workspace_dir == ws_b.resolve()

    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)
    assert "run_coding_agent" in reg.names()
    assert "fetch_web_page" in reg.names()
    assert "read_pdf" in reg.names()

    async def run() -> None:
        md_body = (
            "# Introducción\n\n"
            "Este ensayo incluye **negritas**, *cursivas* y `código`.\n\n"
            "- Punto clave uno\n"
            "- Punto clave dos\n\n"
            "| Métrica | Valor |\n| --- | --- |\n| Precisión | 98% |\n"
        )
        docx_res = await reg.execute(
            "create_document",
            {
                "path": "ensayo_apa.docx",
                "title": "Impacto de la IA en Educación",
                "subtitle": "Estudio comparativo",
                "author": "Adán David",
                "institution": "Universidad Nacional",
                "course": "Metodología",
                "date": "2026",
                "style": "apa",
                "content": md_body,
                "references": ["Russell, S., & Norvig, P. (2021). Artificial Intelligence."],
            },
        )
        assert docx_res["status"] == "ok"
        assert docx_res["style"] == "apa"
        assert (ws_b / "ensayo_apa.docx").exists()

        pdf_res = await reg.execute(
            "create_document",
            {
                "path": "ensayo_apa.pdf",
                "title": "Informe PDF APA",
                "author": "Adán David",
                "style": "apa",
                "content": md_body,
                "references": ["Vaswani, A. et al. (2017). Attention Is All You Need."],
            },
        )
        assert pdf_res["status"] == "ok"
        pdf_read = await reg.execute("read_pdf", {"path": "ensayo_apa.pdf", "max_pages": 5})
        assert pdf_read["status"] == "ok"
        assert "Informe PDF APA" in pdf_read["content"]
        assert "negritas" in pdf_read["content"]

        fake_html = "<html><head><title>Doc Test</title><script>var x=1;</script></head><body><h1>Hello</h1><p>World content</p></body></html>"
        mock_resp = MagicMock(status_code=200, text=fake_html, url="https://example.com/article")
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        with patch("jarvis.tools.research.httpx.AsyncClient", return_value=mock_client):
            web_res = await reg.execute("fetch_web_page", {"url": "https://example.com/article", "max_chars": 1000})
        assert web_res["status"] == "ok"
        assert web_res["title"] == "Doc Test"
        assert "var x=1" not in web_res["content"]
        assert "World content" in web_res["content"]

    asyncio.run(run())


def test_coding_agent_executes_tools_and_respects_step_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    cfg = JarvisConfig(
        gemini_api_key="test-key",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "jarvis.db",
        workspace_dir=tmp_path,
        auto_approve_dangerous=True,
    )
    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)

    call_count = {"n": 0}

    class _FakeModels:
        async def generate_content(self, **_kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                fn = SimpleNamespace(name="write_file", args={"path": "app.py", "content": "print('ok')\n"})
                return SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[]))],
                    function_calls=[fn],
                    text="",
                )
            return SimpleNamespace(
                candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[]))],
                function_calls=[],
                text="Created app.py cleanly.",
            )

    class _FakeGenAIClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.aio = SimpleNamespace(models=_FakeModels())

    monkeypatch.setattr("jarvis.tools.coding_agent.genai.Client", _FakeGenAIClient)

    res = asyncio.run(reg.execute("run_coding_agent", {"task": "Create app.py", "max_steps": 4}))
    assert res["status"] == "ok"
    assert res["steps"] == 2
    assert any(p.endswith("app.py") for p in res["files_changed"])
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert "Created app.py cleanly." in res["summary"]


def test_audio_output_stream_live_level_percent() -> None:
    import numpy as np
    from jarvis.audio import AudioOutputStream

    out = AudioOutputStream(sample_rate=24000, channels=1)
    assert out.level_percent == 0

    tone = (np.ones(960, dtype=np.int16) * 4500).tobytes()
    out.enqueue(tone)
    buf = np.zeros((960, 1), dtype=np.int16)
    out._callback(buf, 960, None, None)
    assert out.level_percent > 50

    out.clear()
    assert out.level_percent == 0


def test_audio_input_defaults_to_shared_wasapi_so_other_apps_keep_mic(monkeypatch: pytest.MonkeyPatch) -> None:
    import sounddevice as sd
    from jarvis.audio import AudioInputStream

    captured_settings: list[object] = []

    class _FakeStream:
        def start(self) -> None:
            pass

    def _fake_input_stream(**kwargs: object) -> _FakeStream:
        captured_settings.append(kwargs.get("extra_settings"))
        return _FakeStream()

    monkeypatch.delenv("JARVIS_WASAPI_EXCLUSIVE", raising=False)
    monkeypatch.setattr("jarvis.audio.get_wasapi_default_device", lambda kind="input": 1)
    monkeypatch.setattr(sd, "InputStream", _fake_input_stream)

    stream = AudioInputStream(sample_rate=16000, channels=1, blocksize=1280)
    stream._open_portaudio_stream()
    assert len(captured_settings) == 1
    assert captured_settings[0] is not None
    assert getattr(captured_settings[0], "exclusive", False) is False


def test_weather_search_and_web_search_loop_breaker(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from jarvis.live_session import JarvisLiveSession
    from jarvis.memory import JarvisMemory
    from jarvis.tools import build_default_registry

    cfg = JarvisConfig(gemini_api_key="dummy-key", db_path=tmp_path / "m.db", data_dir=tmp_path)
    mem = JarvisMemory(cfg.db_path)
    reg = build_default_registry(cfg, mem)

    async def _fake_get(url: str, params: dict[str, object] | None = None):
        if "geocoding-api.open-meteo.com" in url:
            return MagicMock(
                status_code=200,
                json=lambda: {"results": [{"name": "New York", "admin1": "New York", "country": "United States", "latitude": 40.71, "longitude": -74.0}]},
            )
        if "api.open-meteo.com" in url:
            return MagicMock(
                status_code=200,
                json=lambda: {"current": {"temperature_2m": 12.5, "apparent_temperature": 11.0, "relative_humidity_2m": 58, "wind_speed_10m": 14.2}},
            )
        return MagicMock(status_code=404, text="", json=lambda: {})

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.get.side_effect = _fake_get

    async def run() -> None:
        with patch("jarvis.tools.desktop.httpx.AsyncClient", return_value=mock_client):
            wres = await reg.execute("web_search", {"query": "current temperature in New York"})
        assert wres["status"] == "ok"
        assert wres["snippets"]
        assert "12.5°C" in wres["snippets"][0]["snippet"]
        assert "54.5°F" in wres["snippets"][0]["snippet"]

        # Verify loop breaker blocks 3rd+ web_search within same user turn
        session_mgr = JarvisLiveSession(cfg, mem, reg)
        sent: list[object] = []

        class _Sess:
            async def send_tool_response(self, *, function_responses):
                sent.extend(function_responses)

        s = _Sess()
        session_mgr._record_user_utterance("¿En cuánto está la temperatura en New York?")
        with patch("jarvis.tools.desktop.httpx.AsyncClient", return_value=mock_client):
            for i in range(4):
                call = SimpleNamespace(id=f"id-{i}", name="web_search", args={"query": f"weather ny {i}"})
                await session_mgr._execute_and_respond(s, call)

        assert sent[0].response["status"] == "ok"
        assert sent[1].response["status"] == "ok"
        assert sent[2].response["status"] == "limit_reached"
        assert sent[3].response["status"] == "limit_reached"

        # New user prompt resets the search cap
        session_mgr._record_user_utterance("Ahora busca el clima en Madrid")
        with patch("jarvis.tools.desktop.httpx.AsyncClient", return_value=mock_client):
            await session_mgr._execute_and_respond(s, SimpleNamespace(id="id-new", name="web_search", args={"query": "weather madrid"}))
        assert sent[-1].response["status"] == "ok"

    asyncio.run(run())


def test_speaker_echo_does_not_self_barge_in_or_leak_preroll() -> None:
    import numpy as np
    from jarvis.audio import AudioInputStream, EchoCanceller, PlaybackReferenceBuffer

    ref_buf = PlaybackReferenceBuffer(target_rate=16000, max_seconds=3.0)
    t24 = np.linspace(0.0, 0.24, int(24000 * 0.24), endpoint=False)
    speaker_24k = (7000.0 * np.sin(2 * np.pi * 300 * t24)).astype(np.int16)
    ref_buf.push_24k(speaker_24k, source_rate=24000)

    hist, active = ref_buf.snapshot(max_samples=6400)
    assert active is True

    # Simulate distorted/uncorrelated laptop speaker echo (~900 RMS, low correlation rho < 0.22)
    t16 = np.linspace(0.0, 0.08, 1280, endpoint=False)
    uncorrelated_echo = (1200.0 * np.sin(2 * np.pi * 777 * t16)).astype(np.int16)
    aec = EchoCanceller()
    _, dt_uncorr = aec.process(uncorrelated_echo, hist, noise_rms=60.0)
    assert dt_uncorr is False

    # Verify AudioInputStream never triggers self-barge-in on distorted echo
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
    stream._process_dual_branch(uncorrelated_echo)
    stream._process_dual_branch(uncorrelated_echo)
    assert barge_calls == 0
    assert stream.is_speech_active is False
    assert len(stream._pre_roll) == 0


