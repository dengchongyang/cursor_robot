"""
本地持久化记忆存储。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Iterator

from loguru import logger

from config import settings


def _truncate(text: str, limit: int = 120) -> str:
    """截断过长文本，避免记忆摘要失控。"""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class MemoryStore:
    """基于 SQLite 的本地消息与操作日志存储。"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = Path.cwd() / self.db_path
        self.auto_memory_path = Path.cwd() / "memory" / "auto_memory.md"

        self._init_lock = threading.Lock()
        self._initialized = False

    def init_db(self) -> None:
        """初始化数据库和基础表结构。"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)

            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        message_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        chat_type TEXT NOT NULL DEFAULT '',
                        sender_name TEXT NOT NULL DEFAULT '',
                        display_time TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_chat_created_at
                    ON messages(chat_id, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operation_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        sender_name TEXT NOT NULL DEFAULT '',
                        user_message TEXT NOT NULL DEFAULT '',
                        history_excerpt TEXT NOT NULL DEFAULT '',
                        agent_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'received',
                        result_summary TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(chat_id, message_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_operation_logs_chat_created_at
                    ON operation_logs(chat_id, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_chunks (
                        path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL DEFAULT '',
                        modified_at REAL NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(path, chunk_index)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_document_chunks_path
                    ON document_chunks(path)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        category TEXT NOT NULL DEFAULT 'generic',
                        content TEXT NOT NULL DEFAULT '',
                        source_excerpt TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_candidates_chat_created_at
                    ON memory_candidates(chat_id, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS durable_memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL DEFAULT 'chat',
                        scope_id TEXT NOT NULL DEFAULT '',
                        category TEXT NOT NULL DEFAULT 'generic',
                        content TEXT NOT NULL DEFAULT '',
                        source_message_id TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0.5,
                        content_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(scope, scope_id, content_hash)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_durable_memories_scope
                    ON durable_memories(scope, scope_id, updated_at)
                    """
                )
                conn.commit()

            self._initialized = True
            logger.info(f"本地记忆库已就绪 | db={self.db_path}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.init_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_messages(self, chat_id: str, chat_type: str, messages: list[dict]) -> None:
        """持久化聊天消息，用于跨重启恢复上下文。"""
        if not messages:
            return

        now = datetime.utcnow().isoformat(timespec="seconds")
        rows = []
        for index, msg in enumerate(messages):
            message_id = msg.get("message_id") or f"{chat_id}:{now}:{index}"
            rows.append(
                (
                    message_id,
                    chat_id,
                    chat_type,
                    msg.get("sender", "未知用户"),
                    msg.get("time", "unknown"),
                    msg.get("content", ""),
                    msg.get("created_at") or now,
                    now,
                )
            )

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO messages (
                    message_id, chat_id, chat_type, sender_name, display_time, content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    sender_name=excluded.sender_name,
                    display_time=excluded.display_time,
                    content=excluded.content,
                    updated_at=excluded.updated_at
                """,
                rows,
            )

    def get_recent_messages(self, chat_id: str, limit: int = 20) -> list[dict]:
        """获取本地持久化的最近消息。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, sender_name, display_time, content, created_at
                FROM messages
                WHERE chat_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

        messages = []
        for row in reversed(rows):
            messages.append(
                {
                    "message_id": row["message_id"],
                    "time": row["display_time"],
                    "sender": row["sender_name"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                }
            )
        return messages

    def add_memory_candidate(
        self,
        chat_id: str,
        message_id: str,
        category: str,
        content: str,
        source_excerpt: str,
        status: str = "pending",
    ) -> None:
        """记录反思生成的记忆候选，便于后续排查和提升。"""
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_candidates (
                    chat_id, message_id, category, content, source_excerpt, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    category,
                    _truncate(content, 500),
                    _truncate(source_excerpt, 500),
                    status,
                    now,
                ),
            )

    def upsert_durable_memory(
        self,
        scope: str,
        scope_id: str,
        category: str,
        content: str,
        source_message_id: str,
        confidence: float = 0.7,
    ) -> None:
        """写入长期记忆，重复内容会自动合并更新时间。"""
        now = datetime.utcnow().isoformat(timespec="seconds")
        normalized_content = _truncate(content.strip(), 500)
        if not normalized_content:
            return

        content_hash = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO durable_memories (
                    scope, scope_id, category, content, source_message_id, confidence, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, scope_id, content_hash) DO UPDATE SET
                    confidence = MAX(durable_memories.confidence, excluded.confidence),
                    source_message_id = excluded.source_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    scope,
                    scope_id,
                    category,
                    normalized_content,
                    source_message_id,
                    confidence,
                    content_hash,
                    now,
                    now,
                ),
            )
        self.sync_memories_to_markdown()

    def get_durable_memories(self, chat_id: str, limit: int = 8) -> list[dict]:
        """获取与当前会话相关的长期记忆，优先 chat 级，其次 global。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope, scope_id, category, content, confidence, updated_at
                FROM durable_memories
                WHERE (scope = 'chat' AND scope_id = ?)
                   OR (scope = 'global' AND scope_id = 'global')
                ORDER BY
                    CASE WHEN scope = 'chat' THEN 0 ELSE 1 END,
                    confidence DESC,
                    updated_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    def upsert_operation(
        self,
        chat_id: str,
        message_id: str,
        sender_name: str,
        user_message: str,
        history_excerpt: str,
        status: str,
    ) -> None:
        """记录一次任务处理的开始状态。"""
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO operation_logs (
                    chat_id, message_id, sender_name, user_message, history_excerpt, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    sender_name=excluded.sender_name,
                    user_message=excluded.user_message,
                    history_excerpt=excluded.history_excerpt,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    chat_id,
                    message_id,
                    sender_name,
                    _truncate(user_message, 500),
                    _truncate(history_excerpt, 1500),
                    status,
                    now,
                    now,
                ),
            )

    def complete_operation(
        self,
        chat_id: str,
        message_id: str,
        status: str,
        agent_id: str = "",
        result_summary: str = "",
    ) -> None:
        """更新任务处理结果。"""
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE operation_logs
                SET status = ?, agent_id = ?, result_summary = ?, updated_at = ?
                WHERE chat_id = ? AND message_id = ?
                """,
                (status, agent_id, _truncate(result_summary, 500), now, chat_id, message_id),
            )

    def get_recent_operations(self, chat_id: str, limit: int = 5) -> list[dict]:
        """获取会话最近的操作日志。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sender_name, user_message, agent_id, status, result_summary, created_at, updated_at
                FROM operation_logs
                WHERE chat_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    def build_memory_digest(self, chat_id: str, operation_limit: int = 8) -> str:
        """生成供 prompt 使用的持久化记忆摘要。"""
        recent_ops = self.get_recent_operations(chat_id, limit=operation_limit)
        durable_memories = self.get_durable_memories(chat_id, limit=4)

        with self._connect() as conn:
            message_count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            operation_count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM operation_logs WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

        message_count = message_count_row["count"] if message_count_row else 0
        operation_count = operation_count_row["count"] if operation_count_row else 0

        if not recent_ops and not durable_memories and message_count == 0:
            return "（暂无持久化记忆）"

        lines = [
            f"- 该会话已持久化 {message_count} 条消息、{operation_count} 次任务记录。",
        ]

        succeeded = next((op for op in recent_ops if op["status"] == "succeeded"), None)
        failed = next((op for op in recent_ops if op["status"] == "failed"), None)

        if succeeded:
            lines.append(
                f"- 最近一次成功任务：{_truncate(succeeded['user_message'])}。结果：{_truncate(succeeded['result_summary'] or '已成功提交给 Agent')}"
            )
        if failed:
            lines.append(
                f"- 最近一次失败任务：{_truncate(failed['user_message'])}。结果：{_truncate(failed['result_summary'] or '处理失败')}"
            )

        if not succeeded and not failed and recent_ops:
            lines.append(f"- 最近一次记录的请求：{_truncate(recent_ops[0]['user_message'])}")

        if durable_memories:
            lines.append("- 已沉淀的长期记忆：")
            for memory in durable_memories:
                lines.append(
                    f"  [{memory['category']}] {_truncate(memory['content'], 120)}"
                )

        return "\n".join(lines)

    def format_long_term_memories(self, chat_id: str, limit: int = 8) -> str:
        """格式化长期记忆，供 prompt 独立参考。"""
        rows = self.get_durable_memories(chat_id, limit=limit)
        if not rows:
            return "（暂无长期记忆）"

        lines = []
        for row in rows:
            updated_at = row["updated_at"].replace("T", " ")[:16]
            scope_label = "当前会话" if row["scope"] == "chat" else "全局"
            lines.append(
                f"- [{scope_label}/{row['category']}] {row['content']} (updated {updated_at})"
            )
        return "\n".join(lines)

    def sync_memories_to_markdown(self) -> None:
        """将长期记忆导出为 Markdown，便于人工查看和知识检索。"""
        self.auto_memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope, scope_id, category, content, confidence, updated_at
                FROM durable_memories
                ORDER BY
                    CASE WHEN scope = 'global' THEN 0 ELSE 1 END,
                    confidence DESC,
                    updated_at DESC
                LIMIT 200
                """
            ).fetchall()

        lines = [
            "# Auto Memory",
            "",
            "该文件由程序自动生成，请勿手工维护关键内容。",
            "",
        ]

        if not rows:
            lines.append("暂无自动沉淀的长期记忆。")
        else:
            current_section = None
            for row in rows:
                section = "Global" if row["scope"] == "global" else f"Chat {row['scope_id']}"
                if section != current_section:
                    lines.extend(["", f"## {section}", ""])
                    current_section = section
                lines.append(
                    f"- [{row['category']}] {row['content']} | confidence={row['confidence']:.2f} | updated={row['updated_at'].replace('T', ' ')[:16]}"
                )

        self.auto_memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def format_recent_operations(self, chat_id: str, limit: int = 5) -> str:
        """格式化近期操作记录，供 prompt 参考。"""
        rows = self.get_recent_operations(chat_id, limit=limit)
        if not rows:
            return "（暂无近期操作记录）"

        lines = []
        for row in rows:
            created_at = row["created_at"].replace("T", " ")[:16]
            request = _truncate(row["user_message"], 80)
            result = _truncate(row["result_summary"] or "无结果摘要", 80)
            lines.append(
                f"- [{created_at}] status={row['status']} | request={request} | result={result}"
            )
        return "\n".join(lines)


memory_store = MemoryStore(settings.memory_db_path)
