"""
本地文档知识库检索。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from loguru import logger

from runtime_memory import memory_store


def _tokenize(text: str) -> set[str]:
    """对文本进行轻量分词，用于本地关键词匹配。"""
    lowered = text.lower()
    ascii_tokens = set(re.findall(r"[a-z0-9_./-]{2,}", lowered))
    cjk_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    return ascii_tokens | cjk_tokens


def _chunk_markdown(text: str, chunk_size: int = 1200) -> list[str]:
    """按段落切块，保持语义相邻内容尽量在同一块内。"""
    normalized = text.replace("\r\n", "\n")
    paragraphs = [p.strip() for p in normalized.split("\n\n") if p.strip()]

    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= chunk_size:
            current = paragraph
            continue

        lines = paragraph.splitlines()
        piece = ""
        for line in lines:
            candidate = f"{piece}\n{line}".strip() if piece else line
            if len(candidate) <= chunk_size:
                piece = candidate
            else:
                if piece:
                    chunks.append(piece)
                piece = line[:chunk_size]
        if piece:
            current = piece

    if current:
        chunks.append(current)

    return chunks


class KnowledgeRetriever:
    """扫描本地文档目录并构建轻量知识检索。"""

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root)
        self.knowledge_targets = [
            self.workspace_root / "README.md",
            self.workspace_root / "doc",
            self.workspace_root / "memory",
            self.workspace_root / "skills",
        ]
        self.allowed_suffixes = {".md", ".txt", ".rst"}

    def sync(self) -> None:
        """同步知识库，自动增量更新文档切块。"""
        active_paths: set[str] = set()

        for target in self.knowledge_targets:
            if target.is_file():
                self._sync_file(target, active_paths)
            elif target.is_dir():
                for file_path in sorted(target.rglob("*")):
                    if file_path.is_file() and file_path.suffix.lower() in self.allowed_suffixes:
                        self._sync_file(file_path, active_paths)

        self._prune_deleted_files(active_paths)

    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        """根据查询返回最相关的文档片段。"""
        self.sync()
        query_tokens = _tokenize(query)

        if not query_tokens:
            return self._fetch_recent_chunks(limit)

        with memory_store._connect() as conn:  # noqa: SLF001 - same persistence layer by design
            rows = conn.execute(
                """
                SELECT path, chunk_index, title, content
                FROM document_chunks
                """
            ).fetchall()

        scored: list[tuple[int, dict]] = []
        for row in rows:
            content = row["content"]
            path = row["path"]
            title = row["title"]
            haystack_tokens = _tokenize(f"{path} {title} {content}")
            overlap = query_tokens & haystack_tokens
            if not overlap:
                continue

            score = len(overlap)
            if any(token in path.lower() for token in query_tokens):
                score += 2
            if title and any(token in title.lower() for token in query_tokens):
                score += 1

            scored.append(
                (
                    score,
                    {
                        "path": path,
                        "chunk_index": row["chunk_index"],
                        "title": title,
                        "content": content,
                    },
                )
            )

        scored.sort(key=lambda item: (item[0], -item[1]["chunk_index"]), reverse=True)
        return [item[1] for item in scored[:limit]] or self._fetch_recent_chunks(limit)

    def format_for_prompt(self, query: str, limit: int = 5) -> str:
        """格式化检索结果，注入到 prompt。"""
        chunks = self.retrieve(query, limit=limit)
        if not chunks:
            return "（暂无可用文档片段）"

        lines = []
        for chunk in chunks:
            title = f" | {chunk['title']}" if chunk["title"] else ""
            lines.append(f"- 来源: {chunk['path']}#{chunk['chunk_index']}{title}")
            lines.append(chunk["content"])
        return "\n\n".join(lines)

    def _sync_file(self, file_path: Path, active_paths: set[str]) -> None:
        rel_path = file_path.relative_to(self.workspace_root).as_posix()
        active_paths.add(rel_path)
        modified_at = file_path.stat().st_mtime
        raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
        chunks = _chunk_markdown(raw_text)
        title = self._extract_title(raw_text, file_path.name)
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        with memory_store._connect() as conn:  # noqa: SLF001 - same persistence layer by design
            existing = conn.execute(
                """
                SELECT content_hash, modified_at
                FROM document_chunks
                WHERE path = ?
                LIMIT 1
                """,
                (rel_path,),
            ).fetchone()

            if existing and existing["content_hash"] == content_hash and float(existing["modified_at"]) == modified_at:
                return

            conn.execute("DELETE FROM document_chunks WHERE path = ?", (rel_path,))
            now = datetime.utcnow().isoformat(timespec="seconds")
            rows = [
                (
                    rel_path,
                    index,
                    title,
                    chunk,
                    content_hash,
                    modified_at,
                    now,
                )
                for index, chunk in enumerate(chunks)
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO document_chunks (
                        path, chunk_index, title, content, content_hash, modified_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

        logger.debug(f"知识库已同步文档 | path={rel_path} | chunks={len(chunks)}")

    def _prune_deleted_files(self, active_paths: set[str]) -> None:
        with memory_store._connect() as conn:  # noqa: SLF001 - same persistence layer by design
            rows = conn.execute("SELECT DISTINCT path FROM document_chunks").fetchall()
            indexed_paths = {row["path"] for row in rows}
            stale_paths = indexed_paths - active_paths
            for stale_path in stale_paths:
                conn.execute("DELETE FROM document_chunks WHERE path = ?", (stale_path,))

    def _fetch_recent_chunks(self, limit: int) -> list[dict]:
        with memory_store._connect() as conn:  # noqa: SLF001 - same persistence layer by design
            rows = conn.execute(
                """
                SELECT path, chunk_index, title, content
                FROM document_chunks
                ORDER BY updated_at DESC, path ASC, chunk_index ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _extract_title(text: str, fallback: str) -> str:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                return line.lstrip("#").strip()
        return fallback


knowledge_retriever = KnowledgeRetriever(str(Path(__file__).resolve().parent.parent))
