"""SQLite-backed memory, preferences, reminders, and synced conversation log for Jarvis."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_REL_DUE_RE = re.compile(
    r"^(?:en|in|\+)?\s*(\d+)\s*(s|sec|secs|seg|segundo|segundos|m|min|mins|minuto|minutos|h|hr|hrs|hora|horas)\s*$",
    re.IGNORECASE,
)
_TIME_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*(am|pm))?$", re.IGNORECASE)


def normalize_due_at(raw: str | None, *, now: datetime | None = None) -> str | None:
    """Normalize relative ('+10m', 'en 5 minutos') or 'HH:MM' due times into local ISO strings."""
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip()
    ref = now or datetime.now()

    rel_match = _REL_DUE_RE.match(cleaned)
    if rel_match:
        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit.startswith("h"):
            dt = ref + timedelta(hours=amount)
        elif unit.startswith(("m")):
            dt = ref + timedelta(minutes=amount)
        else:
            dt = ref + timedelta(seconds=amount)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    hhmm_match = _TIME_HHMM_RE.match(cleaned)
    if hhmm_match:
        hour = int(hhmm_match.group(1))
        minute = int(hhmm_match.group(2))
        ampm = (hhmm_match.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= ref:
                candidate += timedelta(days=1)
            return candidate.strftime("%Y-%m-%d %H:%M:%S")

    return cleaned


def parse_due_datetime(raw: str | None) -> datetime | None:
    """Best-effort parse of stored `due_at` strings into naive local datetime."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


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
                    notified INTEGER NOT NULL DEFAULT 0,
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
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(reminders)").fetchall()
            }
            if "notified" not in cols:
                conn.execute("ALTER TABLE reminders ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")

            # FTS5 full-text search indexes (token-efficient selective memory retrieval)
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
                    key, value, category, tokenize='unicode61 remove_diacritics 2'
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                    role, content, source, created_at, tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TRIGGER IF NOT EXISTS trg_facts_ai AFTER INSERT ON facts BEGIN
                    INSERT INTO facts_fts(rowid, key, value, category)
                    VALUES (new.rowid, new.key, new.value, new.category);
                END;
                CREATE TRIGGER IF NOT EXISTS trg_facts_au AFTER UPDATE ON facts BEGIN
                    DELETE FROM facts_fts WHERE rowid = old.rowid;
                    INSERT INTO facts_fts(rowid, key, value, category)
                    VALUES (new.rowid, new.key, new.value, new.category);
                END;
                CREATE TRIGGER IF NOT EXISTS trg_facts_ad AFTER DELETE ON facts BEGIN
                    DELETE FROM facts_fts WHERE rowid = old.rowid;
                END;

                CREATE TRIGGER IF NOT EXISTS trg_conv_ai AFTER INSERT ON conversations BEGIN
                    INSERT INTO conversations_fts(rowid, role, content, source, created_at)
                    VALUES (new.id, new.role, new.content, new.source, new.created_at);
                END;
                CREATE TRIGGER IF NOT EXISTS trg_conv_ad AFTER DELETE ON conversations BEGIN
                    DELETE FROM conversations_fts WHERE rowid = old.id;
                END;
                """
            )
            facts_cnt = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            facts_fts_cnt = conn.execute("SELECT COUNT(*) FROM facts_fts").fetchone()[0]
            if facts_cnt > 0 and facts_fts_cnt == 0:
                conn.execute(
                    "INSERT INTO facts_fts(rowid, key, value, category) SELECT rowid, key, value, category FROM facts"
                )
            conv_cnt = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            conv_fts_cnt = conn.execute("SELECT COUNT(*) FROM conversations_fts").fetchone()[0]
            if conv_cnt > 0 and conv_fts_cnt == 0:
                conn.execute(
                    "INSERT INTO conversations_fts(rowid, role, content, source, created_at) "
                    "SELECT id, role, content, source, created_at FROM conversations"
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

    @staticmethod
    def _to_fts_query(query: str) -> str:
        tokens = [t for t in re.findall(r"\w+", query, flags=re.UNICODE) if len(t) >= 2]
        if not tokens:
            tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        seen: list[str] = []
        for tok in tokens:
            low = tok.lower()
            if low not in seen:
                seen.append(low)
            if len(seen) >= 8:
                break
        return " OR ".join(f'"{t}"*' for t in seen)

    def recall_facts(self, query: str | None = None, limit: int = 20) -> list[MemoryFact]:
        """Search remembered facts via FTS5 (with LIKE fallback) or return recent entries."""
        with self._connect() as conn:
            rows = []
            if query and query.strip():
                fts_q = self._to_fts_query(query)
                if fts_q:
                    try:
                        rows = conn.execute(
                            """
                            SELECT f.key, f.value, f.category, f.updated_at
                            FROM facts_fts
                            JOIN facts f ON f.rowid = facts_fts.rowid
                            WHERE facts_fts MATCH ?
                            ORDER BY bm25(facts_fts), f.updated_at DESC
                            LIMIT ?
                            """,
                            (fts_q, limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                if not rows:
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

    def search_memory(self, query: str, limit: int = 6) -> dict[str, Any]:
        """Search both remembered facts and past conversations using SQLite FTS5."""
        cleaned = query.strip()
        capped = max(1, min(int(limit), 8))
        if not cleaned:
            return {"facts": [], "conversations": []}

        facts = self.recall_facts(query=cleaned, limit=capped)
        fts_q = self._to_fts_query(cleaned)
        conv_matches: list[dict[str, str]] = []
        with self._connect() as conn:
            rows = []
            if fts_q:
                try:
                    rows = conn.execute(
                        """
                        SELECT c.role, c.content, c.source, c.created_at
                        FROM conversations_fts
                        JOIN conversations c ON c.id = conversations_fts.rowid
                        WHERE conversations_fts MATCH ?
                        ORDER BY bm25(conversations_fts), c.id DESC
                        LIMIT ?
                        """,
                        (fts_q, capped),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = f"%{cleaned}%"
                rows = conn.execute(
                    """
                    SELECT role, content, source, created_at
                    FROM conversations
                    WHERE content LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (like, capped),
                ).fetchall()
            for row in rows:
                text = str(row["content"])
                conv_matches.append(
                    {
                        "role": str(row["role"]),
                        "snippet": text[:160] + ("..." if len(text) > 160 else ""),
                        "created_at": str(row["created_at"]),
                    }
                )

        return {
            "facts": [
                {"key": f.key, "value": f.value[:160], "category": f.category}
                for f in facts
            ],
            "conversations": conv_matches,
        }

    def delete_fact(self, key: str) -> bool:
        """Delete a remembered fact by key. Returns True if a row was removed."""
        norm_key = key.strip().lower()
        if not norm_key:
            return False
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM facts WHERE key = ?", (norm_key,))
            deleted = cursor.rowcount > 0
        if deleted:
            self.export_sync_markdown()
        return deleted

    # --- Reminders / Tasks ---

    def add_reminder(self, title: str, due_at: str | None = None) -> Reminder:
        """Create a new reminder or to-do item."""
        now = _utc_now_iso()
        clean_due = normalize_due_at(due_at)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO reminders (title, due_at, completed, notified, created_at) VALUES (?, ?, 0, 0, ?)",
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

    def claim_due_reminders(self, *, now: datetime | None = None) -> list[Reminder]:
        """Return uncompleted reminders whose `due_at` <= now and mark them notified once."""
        ref = now or datetime.now()
        due_items: list[Reminder] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, due_at, completed, created_at
                FROM reminders
                WHERE completed = 0 AND notified = 0 AND due_at IS NOT NULL AND TRIM(due_at) != ''
                ORDER BY id ASC
                """
            ).fetchall()
            claimed_ids: list[int] = []
            for row in rows:
                due_dt = parse_due_datetime(row["due_at"])
                if due_dt is not None and due_dt <= ref:
                    claimed_ids.append(int(row["id"]))
                    due_items.append(
                        Reminder(
                            id=row["id"],
                            title=row["title"],
                            due_at=row["due_at"],
                            completed=bool(row["completed"]),
                            created_at=row["created_at"],
                        )
                    )
            if claimed_ids:
                placeholders = ",".join("?" for _ in claimed_ids)
                conn.execute(
                    f"UPDATE reminders SET notified = 1 WHERE id IN ({placeholders})",
                    claimed_ids,
                )
        return due_items

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
        """Format compact remembered facts, open reminders, and recent turns for session prompt injection."""
        facts = self.recall_facts(limit=8)
        reminders = self.list_reminders(include_completed=False)
        recent = self.recent_messages(limit=4)

        sections: list[str] = []
        if facts:
            fact_lines = [f"- {f.key}: {f.value} ({f.category})" for f in facts]
            sections.append("Known user facts & preferences:\n" + "\n".join(fact_lines))
        if reminders:
            rem_lines = [
                f"- [#{r.id}] {r.title}" + (f" (due: {r.due_at})" if r.due_at else "")
                for r in reminders[:6]
            ]
            sections.append("Open reminders:\n" + "\n".join(rem_lines))
        if recent:
            conv_lines = [f"- [{m.role}] {m.content[:140]}" for m in recent]
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
