"""SQLite-backed memory, preferences, reminders, and synced conversation log for Jarvis."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Reminder:
    """A persistent reminder or task tracked by Jarvis."""

    id: int
    title: str
    due_at: str | None
    completed: bool
    created_at: str


@dataclass(slots=True)
class MemoryFact:
    """A remembered user preference or fact."""

    key: str
    value: str
    category: str
    updated_at: str


@dataclass(slots=True)
class ConversationTurn:
    """A persisted user or Jarvis message turn for cross-session sync."""

    id: int
    source: str
    role: str
    content: str
    created_at: str


class JarvisMemory:
    """Thread-safe SQLite store for facts, reminders, conversation history, and audit logs."""

    def __init__(self, db_path: Path, sync_md_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_md_path = (
            Path(sync_md_path)
            if sync_md_path is not None
            else self.db_path.parent / "SYNC.md"
        )
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    due_at TEXT,
                    completed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL DEFAULT 'voice',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    summary TEXT NOT NULL DEFAULT '',
                    actions_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # --- Facts / Long-Term Memory ---

    def remember_fact(self, key: str, value: str, category: str = "general") -> MemoryFact:
        """Insert or update a remembered fact or user preference."""
        now = _utc_now_iso()
        norm_key = key.strip().lower()
        clean_cat = category.strip() or "general"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO facts (key, value, category, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = excluded.updated_at
                """,
                (norm_key, value.strip(), clean_cat, now),
            )
        self.export_sync_markdown()
        return MemoryFact(key=norm_key, value=value.strip(), category=clean_cat, updated_at=now)

    def recall_facts(self, query: str | None = None, limit: int = 20) -> list[MemoryFact]:
        """Search remembered facts by substring or return the most recent entries."""
        with self._connect() as conn:
            if query and query.strip():
                like = f"%{query.strip()}%"
                rows = conn.execute(
                    """
                    SELECT key, value, category, updated_at
                    FROM facts
                    WHERE key LIKE ? OR value LIKE ? OR category LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (like, like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT key, value, category, updated_at
                    FROM facts
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [
            MemoryFact(
                key=row["key"],
                value=row["value"],
                category=row["category"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    # --- Reminders / Tasks ---

    def add_reminder(self, title: str, due_at: str | None = None) -> Reminder:
        """Create a new reminder or to-do item."""
        now = _utc_now_iso()
        clean_due = due_at.strip() if due_at else None
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reminders (title, due_at, completed, created_at) VALUES (?, ?, 0, ?)",
                (title.strip(), clean_due, now),
            )
            reminder_id = int(cursor.lastrowid)
        self.export_sync_markdown()
        return Reminder(
            id=reminder_id,
            title=title.strip(),
            due_at=clean_due,
            completed=False,
            created_at=now,
        )

    def list_reminders(self, include_completed: bool = False) -> list[Reminder]:
        """List active (or all) reminders."""
        with self._connect() as conn:
            if include_completed:
                rows = conn.execute(
                    "SELECT id, title, due_at, completed, created_at FROM reminders ORDER BY completed ASC, id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title, due_at, completed, created_at FROM reminders WHERE completed = 0 ORDER BY id DESC"
                ).fetchall()
        return [
            Reminder(
                id=row["id"],
                title=row["title"],
                due_at=row["due_at"],
                completed=bool(row["completed"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def complete_reminder(self, reminder_id: int) -> bool:
        """Mark a reminder as completed. Returns True if a row was updated."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET completed = 1 WHERE id = ?",
                (reminder_id,),
            )
            updated = cursor.rowcount > 0
        if updated:
            self.export_sync_markdown()
        return updated

    # --- Conversation Sync ---

    def log_message(self, role: str, content: str, source: str = "voice") -> ConversationTurn | None:
        """Persist a user or Jarvis conversation turn and refresh SYNC.md."""
        cleaned = content.strip()
        if not cleaned:
            return None
        now = _utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO conversations (source, role, content, created_at) VALUES (?, ?, ?, ?)",
                (source.strip() or "voice", role.strip(), cleaned, now),
            )
            turn_id = int(cursor.lastrowid)
        self.export_sync_markdown()
        return ConversationTurn(
            id=turn_id,
            source=source.strip() or "voice",
            role=role.strip(),
            content=cleaned,
            created_at=now,
        )

    def recent_messages(self, limit: int = 20) -> list[ConversationTurn]:
        """Return the most recent conversation turns in chronological order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, role, content, created_at
                FROM conversations
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ConversationTurn(
                id=row["id"],
                source=row["source"],
                role=row["role"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]

    def log_tool_call(self, tool_name: str, args_json: str, status: str) -> None:
        """Record a tool execution in the local audit log."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (tool_name, args_json, status, created_at) VALUES (?, ?, ?, ?)",
                (tool_name, args_json, status, _utc_now_iso()),
            )

    # --- Task Runs (Antigravity-style Action Feed) ---

    def upsert_task_run(
        self,
        task_id: str,
        prompt: str,
        status: str,
        summary: str,
        actions: list[dict],
    ) -> dict:
        """Insert or update a task execution card with its actions and summary."""
        now = _utc_now_iso()
        actions_json = json.dumps(actions, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (task_id, prompt, status, summary, actions_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    prompt = excluded.prompt,
                    status = excluded.status,
                    summary = excluded.summary,
                    actions_json = excluded.actions_json,
                    updated_at = excluded.updated_at
                """,
                (task_id, prompt.strip(), status, summary.strip(), actions_json, now, now),
            )
            row = conn.execute(
                "SELECT task_id, prompt, status, summary, actions_json, created_at, updated_at FROM task_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return {
            "task_id": row["task_id"],
            "prompt": row["prompt"],
            "status": row["status"],
            "summary": row["summary"],
            "actions": json.loads(row["actions_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def recent_task_runs(self, limit: int = 10) -> list[dict]:
        """Return the most recent task runs in chronological order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, prompt, status, summary, actions_json, created_at, updated_at
                FROM task_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "prompt": row["prompt"],
                "status": row["status"],
                "summary": row["summary"],
                "actions": json.loads(row["actions_json"] or "[]"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in reversed(rows)
        ]

    def clear_chat_history(self) -> None:
        """Clear transient chat messages and task cards while preserving facts and reminders."""
        with self._connect() as conn:
            conn.execute("DELETE FROM conversations")
            conn.execute("DELETE FROM task_runs")
        self.export_sync_markdown()

    def build_context_summary(self) -> str:
        """Format remembered facts, open reminders, and recent turns for session prompt injection."""
        facts = self.recall_facts(limit=15)
        reminders = self.list_reminders(include_completed=False)
        recent = self.recent_messages(limit=8)

        sections: list[str] = []
        if facts:
            fact_lines = [f"- {f.key}: {f.value} ({f.category})" for f in facts]
            sections.append("Known user facts & preferences:\n" + "\n".join(fact_lines))
        if reminders:
            rem_lines = [
                f"- [#{r.id}] {r.title}" + (f" (due: {r.due_at})" if r.due_at else "")
                for r in reminders[:10]
            ]
            sections.append("Open reminders:\n" + "\n".join(rem_lines))
        if recent:
            conv_lines = [f"- [{m.role}] {m.content[:220]}" for m in recent]
            sections.append("Recent conversation context:\n" + "\n".join(conv_lines))
        return "\n\n".join(sections)

    def export_sync_markdown(self) -> None:
        """Write a human-readable memory & conversation snapshot to `data/SYNC.md`."""
        facts = self.recall_facts(limit=30)
        reminders = self.list_reminders(include_completed=False)
        recent = self.recent_messages(limit=20)

        lines = [
            "# Jarvis Synced Memory",
            "",
            f"_Last updated: {_utc_now_iso()}_",
            "",
            "## Open Reminders & Outstanding Tasks",
        ]
        if reminders:
            for r in reminders:
                due = f" *(due: {r.due_at})*" if r.due_at else ""
                lines.append(f"- [ ] **#{r.id}** {r.title}{due}")
        else:
            lines.append("- _None_")

        lines.extend(["", "## Remembered Facts & Preferences"])
        if facts:
            for f in facts:
                lines.append(f"- **{f.key}** (`{f.category}`): {f.value}")
        else:
            lines.append("- _None_")

        lines.extend(["", "## Recent Voice & Text Conversations"])
        if recent:
            for m in recent:
                lines.append(f"- `{m.created_at}` **[{m.source}/{m.role}]** {m.content}")
        else:
            lines.append("- _None_")

        self.sync_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
