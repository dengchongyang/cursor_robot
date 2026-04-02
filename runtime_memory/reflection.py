"""
轻量反思与长期记忆提炼。
"""

from __future__ import annotations

import re

from .store import memory_store


def _normalize_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def _extract_preference_memories(user_message: str) -> list[tuple[str, str, float]]:
    memories: list[tuple[str, str, float]] = []
    text = user_message.strip()

    if not text:
        return memories

    preference_rules = [
        (r"(请用中文|中文回复|以后.*中文)", "preference", "用户偏好中文回复。", 0.9),
        (r"(简洁一点|简短一点|别太长|简单一点)", "preference", "用户偏好简洁直接的回复。", 0.85),
        (r"(详细一点|展开讲|说详细点|尽量详细)", "preference", "用户偏好更详细的解释和步骤。", 0.85),
        (r"(直接给我参数|直接给命令|给我可执行命令)", "preference", "用户偏好直接可执行的结论、参数或命令。", 0.8),
    ]

    for pattern, category, content, confidence in preference_rules:
        if re.search(pattern, text):
            memories.append((category, content, confidence))

    explicit_patterns = [
        (r"(记住[:：]?\s*.+)", "explicit_memory", 0.95),
        (r"(以后[^。！？\n]{0,80})", "explicit_memory", 0.88),
        (r"(默认[^。！？\n]{0,80})", "convention", 0.82),
        (r"(请勿[^。！？\n]{0,80}|不要[^。！？\n]{0,80})", "constraint", 0.85),
    ]

    for pattern, category, confidence in explicit_patterns:
        match = re.search(pattern, text)
        if match:
            content = _normalize_sentence(match.group(1))
            if len(content) >= 4:
                memories.append((category, content, confidence))

    return memories


def _extract_convention_memories(user_message: str) -> list[tuple[str, str, float]]:
    memories: list[tuple[str, str, float]] = []
    text = user_message.strip()

    convention_patterns = [
        (r"(dev 分支|默认分支|main 分支)", "convention", 0.8),
        (r"(环境变量|\.env|配置项)", "convention", 0.72),
        (r"(模型|CURSOR_MODEL|gemini|claude|gpt)", "convention", 0.72),
        (r"(数据库|sqlite|mysql|redis)", "convention", 0.72),
        (r"(部署|上线|发布|重启服务)", "convention", 0.72),
    ]

    for pattern, category, confidence in convention_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            content = _normalize_sentence(text)
            if len(content) >= 6:
                memories.append((category, content, confidence))
                break

    return memories


def _extract_todo_memories(user_message: str, status: str, result_summary: str) -> list[tuple[str, str, float]]:
    memories: list[tuple[str, str, float]] = []
    text = user_message.strip()

    if re.search(r"(待办|TODO|后续|稍后|下次|记一下)", text, flags=re.IGNORECASE):
        memories.append(("todo", _normalize_sentence(text), 0.78))

    if status == "failed":
        summary = result_summary.strip() or "处理失败，后续需要排查。"
        memories.append(("todo", f"曾有失败任务待排查：{_normalize_sentence(text or summary)}", 0.74))

    return memories


def reflect_and_store(
    chat_id: str,
    message_id: str,
    user_message: str,
    status: str,
    result_summary: str,
) -> None:
    """根据用户消息和处理结果生成候选记忆，并提升高价值事实。"""
    candidates: list[tuple[str, str, float]] = []
    candidates.extend(_extract_preference_memories(user_message))
    candidates.extend(_extract_convention_memories(user_message))
    candidates.extend(_extract_todo_memories(user_message, status, result_summary))

    if not candidates:
        generic_summary = _normalize_sentence(
            f"请求：{user_message or '未知请求'}；结果：{result_summary or status}"
        )
        memory_store.add_memory_candidate(
            chat_id=chat_id,
            message_id=message_id,
            category="generic",
            content=generic_summary,
            source_excerpt=user_message or result_summary,
            status="recorded",
        )
        return

    seen: set[tuple[str, str]] = set()
    for category, content, confidence in candidates:
        key = (category, content)
        if key in seen:
            continue
        seen.add(key)

        candidate_status = "promoted" if confidence >= 0.8 else "recorded"
        memory_store.add_memory_candidate(
            chat_id=chat_id,
            message_id=message_id,
            category=category,
            content=content,
            source_excerpt=user_message or result_summary,
            status=candidate_status,
        )

        if confidence >= 0.8:
            scope = "global" if category == "preference" else "chat"
            scope_id = "global" if scope == "global" else chat_id
            memory_store.upsert_durable_memory(
                scope=scope,
                scope_id=scope_id,
                category=category,
                content=content,
                source_message_id=message_id,
                confidence=confidence,
            )
