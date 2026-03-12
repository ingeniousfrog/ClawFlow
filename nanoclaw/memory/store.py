"""SQLite-backed persistent memory with full-text search.
数据层：使用 SQLite 存储对话历史和长期记忆，支持全文搜索和高并发访问。
1、提供持久化会话历史（messages）和长期记忆（memories）。
2、提供记忆检索（FTS5）。
3、顺带维护 cron_jobs 和基础 audit_log 表初始化。

Notes：记忆检索比纯 LIKE 更快、更相关（FTS5）。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Optional

from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)


class MemoryStore:
    """SQLite-backed persistent memory with full-text search."""

    def __init__(self, db_path: str | Path):
        """
        Initialize MemoryStore.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create tables if not exist."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            -- Conversation history
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id);

            -- Long-term memory (facts about user)
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'other',
                created_at TEXT DEFAULT (datetime('now')),
                last_accessed TEXT DEFAULT (datetime('now'))
            );

            -- Full-text search on memories
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, category, content='memories', content_rowid='id'
            );

            -- Triggers to keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, category)
                VALUES (new.id, new.content, new.category);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category)
                VALUES('delete', old.id, old.content, old.category);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, category)
                VALUES('delete', old.id, old.content, old.category);
                INSERT INTO memories_fts(rowid, content, category)
                VALUES (new.id, new.content, new.category);
            END;

            -- Cron jobs
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                cron_expr TEXT,
                interval_seconds INTEGER,
                channel TEXT DEFAULT 'telegram',
                target_id TEXT DEFAULT '',
                quiet_start TEXT DEFAULT '',
                quiet_end TEXT DEFAULT '',
                last_run TEXT,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- Audit log (also created by audit.py, but ensure exists)
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT,
                action_type TEXT NOT NULL,
                tool_name TEXT,
                input_summary TEXT,
                output_summary TEXT,
                status TEXT NOT NULL DEFAULT 'success',
                tokens_used INTEGER DEFAULT 0,
                execution_ms INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_log(timestamp);

            -- Enable WAL mode for better concurrency
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
        """)
        cron_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()
        }
        if "target_id" not in cron_columns:
            conn.execute("ALTER TABLE cron_jobs ADD COLUMN target_id TEXT DEFAULT ''")
        if "quiet_start" not in cron_columns:
            conn.execute("ALTER TABLE cron_jobs ADD COLUMN quiet_start TEXT DEFAULT ''")
        if "quiet_end" not in cron_columns:
            conn.execute("ALTER TABLE cron_jobs ADD COLUMN quiet_end TEXT DEFAULT ''")
        conn.commit()
        conn.close()

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
    ) -> None:
        """
        Add a message to conversation history.

        Args:
            session_id: Session identifier
            role: Message role (user, assistant, tool)
            content: Message content
            tool_name: Optional tool name for tool messages
        """

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, tool_name)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content, tool_name),
            )
            conn.commit()
            conn.close()
        # 把sqlite同步操作丢到线程，避免阻塞事件循环。SQLite 在多线程环境下表现更好，尤其是写操作较多时。
        await asyncio.to_thread(_insert)

    async def get_history(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        """
        Get recent conversation history for a session.

        Args:
            session_id: Session identifier
            limit: Maximum number of messages (default 50)

        Returns:
            List of message dictionaries
        """

        def _query() -> list[dict]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = cursor.fetchall()
            conn.close()
            # Return in chronological order
            return [dict(row) for row in reversed(rows)]

        return await asyncio.to_thread(_query)

    async def save_memory(
        self, content: str, category: str = "other"
    ) -> None:
        """
        Save a fact to long-term memory.

        Deduplicates: skips if very similar fact exists.

        Args:
            content: Fact content
            category: Category (personal, work, preference, project, other)
        """

        def _insert() -> None:
            conn = sqlite3.connect(self.db_path)

            # Check for duplicates (simple substring check)
            cursor = conn.execute(
                """
                SELECT COUNT(*) FROM memories
                WHERE content = ? OR content LIKE ?
                """,
                (content, f"%{content[:50]}%"),
            )
            count = cursor.fetchone()[0]

            if count == 0:
                conn.execute(
                    """
                    INSERT INTO memories (content, category)
                    VALUES (?, ?)
                    """,
                    (content, category),
                )
                conn.commit()
            conn.close()

        await asyncio.to_thread(_insert)

    async def search_memories(
        self, query: str, limit: int = 10
    ) -> list[dict]:
        """
        先将自然语言转FTS查询，不可用时退化到LIKE。
        Search memories using FTS5 full-text search.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching memory dictionaries
        """

        def _fts_query(q: str) -> str:
            """Convert natural language to FTS5 query."""
            import re
            # Remove FTS5 special characters
            clean = re.sub(r'["\*\?\(\)\+\-\~\^\:\.]', ' ', q)
            words = clean.split()
            # Join with OR for broader matches, filter short words
            valid_words = [w for w in words if len(w) > 2 and w.isalnum()]
            return " OR ".join(valid_words)

        def _search() -> list[dict]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            fts_query = _fts_query(query)
            if not fts_query:
                # Fallback to LIKE search for short queries
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE content LIKE ?
                    ORDER BY last_accessed DESC
                    LIMIT ?
                    """,
                    (f"%{query}%", limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT m.* FROM memories m
                    JOIN memories_fts fts ON m.id = fts.rowid
                    WHERE memories_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                )

            rows = cursor.fetchall()

            # Update last_accessed for returned memories
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"""
                    UPDATE memories
                    SET last_accessed = datetime('now')
                    WHERE id IN ({placeholders})
                    """,
                    ids,
                )
                conn.commit()

            conn.close()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_search)

    async def get_all_memories(self) -> list[dict]:
        """
        Get all memories (for dashboard).

        Returns:
            List of all memory dictionaries
        """

        def _query() -> list[dict]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC"
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_query)

    async def delete_memory(self, memory_id: int) -> None:
        """
        Delete a specific memory.

        Args:
            memory_id: Memory ID to delete
        """

        def _delete() -> None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
            conn.close()

        await asyncio.to_thread(_delete)

    async def clear_memories(self) -> None:
        """Clear all memories (user-initiated)."""

        def _clear() -> None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM memories")
            conn.commit()
            conn.close()

        await asyncio.to_thread(_clear)

    async def get_stats(self) -> dict:
        """
        Get memory stats.

        Returns:
            Dictionary with statistics
        """

        def _query() -> dict:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM messages) as total_messages,
                    (SELECT COUNT(DISTINCT session_id) FROM messages) as sessions,
                    (SELECT COUNT(*) FROM memories) as memories,
                    (SELECT COUNT(*) FROM cron_jobs WHERE enabled = 1) as cron_jobs
                """
            )
            row = cursor.fetchone()
            conn.close()
            return {
                "total_messages": row[0] or 0,
                "sessions": row[1] or 0,
                "memories": row[2] or 0,
                "cron_jobs": row[3] or 0,
            }

        return await asyncio.to_thread(_query)


# Global instance
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """Get the global MemoryStore instance."""
    global _memory_store
    if _memory_store is None:
        from nanoclaw.core.config import get_data_path

        _memory_store = MemoryStore(get_data_path() / "nanoclaw.db")
    return _memory_store


def set_memory_store(store: MemoryStore) -> None:
    """Set the global MemoryStore instance."""
    global _memory_store
    _memory_store = store
