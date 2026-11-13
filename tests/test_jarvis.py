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

