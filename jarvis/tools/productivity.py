"""Productivity, persistent memory, reminders, and webhook tools."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import httpx

from jarvis.config import JarvisConfig
from jarvis.memory import JarvisMemory
from jarvis.tools.registry import ToolRegistry, ToolSpec


def register_productivity_tools(
    registry: ToolRegistry,
    config: JarvisConfig,
    memory: JarvisMemory,
) -> None:
    """Register memory, reminder, and webhook automation tools."""

    def remember_fact(key: str, value: str, category: str = "general") -> dict[str, Any]:
        fact = memory.remember_fact(key=key, value=value, category=category)
        return {"status": "ok", "fact": asdict(fact)}

    def recall_facts(query: str = "", limit: int = 20) -> dict[str, Any]:
        facts = memory.recall_facts(query=query or None, limit=max(1, min(int(limit), 50)))
        return {"status": "ok", "facts": [asdict(f) for f in facts]}

    def search_memory(query: str, limit: int = 5) -> dict[str, Any]:
        res = memory.search_memory(query=query, limit=max(1, min(int(limit), 8)))
        return {"status": "ok", "query": query.strip(), **res}

    def manage_reminders(
        action: str,
        title: str = "",
        due_at: str = "",
        reminder_id: int = 0,
        include_completed: bool = False,
    ) -> dict[str, Any]:
        act = action.strip().lower()
        if act == "add":
            if not title.strip():
                return {"status": "error", "error": "Title is required when adding a reminder."}
            rem = memory.add_reminder(title=title, due_at=due_at or None)
            return {"status": "ok", "reminder": asdict(rem)}
        if act == "list":
            items = memory.list_reminders(include_completed=include_completed)
            return {"status": "ok", "reminders": [asdict(r) for r in items]}
        if act == "complete":
            if int(reminder_id) <= 0:
                return {"status": "error", "error": "Valid reminder_id is required to complete."}
            updated = memory.complete_reminder(int(reminder_id))
            return {
                "status": "ok" if updated else "not_found",
                "reminder_id": int(reminder_id),
                "completed": updated,
            }
        return {"status": "error", "error": f"Unsupported action: {action}"}

    async def trigger_webhook(
        target: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = config.webhooks.get(target.strip(), target.strip())
        if not url.lower().startswith(("http://", "https://")):
            known = list(config.webhooks.keys())
            return {
                "status": "error",
                "error": (
                    f"'{target}' is neither a configured webhook alias nor an http(s) URL. "
                    f"Configured aliases: {known}"
                ),
            }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload or {})
        return {
            "status": "ok" if response.is_success else "failed",
            "http_status": response.status_code,
            "url": url,
            "response_preview": response.text[:1000],
        }

    registry.register(
        ToolSpec(
            name="remember_fact",
            description="Persist a user preference, detail, or fact in long-term SQLite memory.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "key": {"type": "STRING", "description": "Short identifier key, e.g. 'editor' or 'coffee_order'."},
                    "value": {"type": "STRING", "description": "Value or detail to remember."},
                    "category": {"type": "STRING", "description": "Optional category such as 'preference', 'work', 'system'."},
                },
                "required": ["key", "value"],
            },
            handler=remember_fact,
        )
    )
    registry.register(
        ToolSpec(
            name="recall_facts",
            description="Search or list remembered user facts and preferences.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING", "description": "Optional keyword filter."},
                    "limit": {"type": "INTEGER"},
                },
            },
            handler=recall_facts,
        )
    )
    registry.register(
        ToolSpec(
            name="search_memory",
            description="Full-text search across past conversations and stored user facts using SQLite FTS5.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING", "description": "Keywords or topic to look up in memory."},
                    "limit": {"type": "INTEGER", "description": "Max items per category (1-8, default 5)."},
                },
                "required": ["query"],
            },
            handler=search_memory,
        )
    )
    registry.register(
        ToolSpec(
            name="manage_reminders",
            description="Add, list, or complete persistent reminders and to-do items.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "action": {"type": "STRING", "enum": ["add", "list", "complete"]},
                    "title": {"type": "STRING", "description": "Reminder description (for 'add')."},
                    "due_at": {"type": "STRING", "description": "Optional due date/time string (for 'add')."},
                    "reminder_id": {"type": "INTEGER", "description": "ID to mark done (for 'complete')."},
                    "include_completed": {"type": "BOOLEAN", "description": "Used with 'list'."},
                },
                "required": ["action"],
            },
            handler=manage_reminders,
        )
    )
    registry.register(
        ToolSpec(
            name="trigger_webhook",
            description="Send a JSON POST request to a configured webhook alias (like n8n) or an HTTP/HTTPS webhook URL.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "target": {"type": "STRING", "description": "Webhook alias from JARVIS_WEBHOOKS or a full URL."},
                    "payload": {"type": "OBJECT", "description": "JSON payload to send."},
                },
                "required": ["target"],
            },
            handler=trigger_webhook,
        )
    )
