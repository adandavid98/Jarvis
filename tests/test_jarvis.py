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



