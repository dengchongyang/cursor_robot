"""
Cursor Agent 后台状态轮询。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from loguru import logger

from config import settings
from runtime_memory import memory_store, reflect_and_store

from .agent import CursorAgent

_active_polls: set[tuple[str, str, str]] = set()
_poll_lock = threading.Lock()


def _normalize_status(raw_status: str) -> str:
    return (raw_status or "").strip().upper()


def _is_terminal_status(status: str) -> bool:
    return status in {"COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED", "ERROR", "TIMED_OUT"}


def _is_success_status(status: str) -> bool:
    return status in {"COMPLETED", "SUCCEEDED"}


def _build_completion_message(cursor_url: str) -> str:
    if cursor_url:
        return f"任务处理完成。如果飞书详细结果暂时没到，可以直接查看 Cursor：{cursor_url}"
    return "任务处理完成。如果飞书详细结果暂时没到，可以稍后再看一下。"


def _build_failure_message(status: str, cursor_url: str) -> str:
    suffix = f" 可在 Cursor 查看详情：{cursor_url}" if cursor_url else ""
    return f"任务处理结束，但状态为 {status}。{suffix}".strip()


def _build_timeout_message(cursor_url: str) -> str:
    suffix = f" 你也可以到 Cursor 查看当前状态：{cursor_url}" if cursor_url else ""
    return f"任务处理时间较长，后台仍可能在继续执行。{suffix}".strip()


def start_agent_polling(
    chat_id: str,
    message_id: str,
    agent_id: str,
    notify: Callable[[str], None],
) -> None:
    """启动后台轮询线程，追踪 Agent 真正终态。"""
    if not agent_id:
        return

    key = (chat_id, message_id, agent_id)
    with _poll_lock:
        if key in _active_polls:
            return
        _active_polls.add(key)

    thread = threading.Thread(
        target=_poll_agent_status,
        args=(chat_id, message_id, agent_id, notify, key),
        daemon=True,
    )
    thread.start()


def _poll_agent_status(
    chat_id: str,
    message_id: str,
    agent_id: str,
    notify: Callable[[str], None],
    poll_key: tuple[str, str, str],
) -> None:
    agent = CursorAgent()
    started_at = time.time()
    deadline = started_at + settings.agent_poll_timeout_seconds
    last_status = ""
    last_url = ""

    try:
        while time.time() < deadline:
            status_data = agent.get_status(agent_id)
            if status_data:
                raw_status = status_data.get("status", "")
                normalized_status = _normalize_status(raw_status) or "UNKNOWN"
                target = status_data.get("target") or {}
                cursor_url = target.get("url", "") or ""
                effective_url = cursor_url or last_url

                if normalized_status != last_status or effective_url != last_url:
                    memory_store.set_chat_session(
                        chat_id=chat_id,
                        agent_id=agent_id,
                        status=normalized_status,
                        cursor_url=effective_url,
                    )
                    memory_store.update_operation_polling(
                        chat_id=chat_id,
                        message_id=message_id,
                        polled_status=normalized_status,
                        cursor_url=effective_url,
                    )
                    logger.info(
                        f"Agent 状态更新 | agent_id={agent_id} | msg_id={message_id} | status={normalized_status}"
                    )
                    last_status = normalized_status
                    last_url = effective_url

                if _is_terminal_status(normalized_status):
                    if _is_success_status(normalized_status):
                        summary = f"Agent 已完成 | status={normalized_status}"
                        if cursor_url:
                            summary += f" | url={cursor_url}"
                        memory_store.complete_operation(
                            chat_id=chat_id,
                            message_id=message_id,
                            status="completed",
                            agent_id=agent_id,
                            result_summary=summary,
                            cursor_url=cursor_url or last_url,
                            polled_status=normalized_status,
                        )
                        memory_store.set_chat_session(
                            chat_id=chat_id,
                            agent_id=agent_id,
                            status=normalized_status,
                            cursor_url=cursor_url or last_url,
                        )
                        reflect_and_store(
                            chat_id=chat_id,
                            message_id=message_id,
                            user_message="",
                            status="completed",
                            result_summary=summary,
                        )
                        if settings.notify_on_agent_completion:
                            notify(_build_completion_message(cursor_url or last_url))
                            memory_store.update_operation_polling(
                                chat_id=chat_id,
                                message_id=message_id,
                                polled_status=normalized_status,
                                cursor_url=cursor_url or last_url,
                                notify_state="completion_notified",
                            )
                    else:
                        summary = f"Agent 终止 | status={normalized_status}"
                        if cursor_url:
                            summary += f" | url={cursor_url}"
                        memory_store.complete_operation(
                            chat_id=chat_id,
                            message_id=message_id,
                            status="failed",
                            agent_id=agent_id,
                            result_summary=summary,
                            cursor_url=cursor_url or last_url,
                            polled_status=normalized_status,
                        )
                        memory_store.set_chat_session(
                            chat_id=chat_id,
                            agent_id=agent_id,
                            status=normalized_status,
                            cursor_url=cursor_url or last_url,
                        )
                        reflect_and_store(
                            chat_id=chat_id,
                            message_id=message_id,
                            user_message="",
                            status="failed",
                            result_summary=summary,
                        )
                        if settings.notify_on_agent_failure:
                            notify(_build_failure_message(normalized_status, cursor_url or last_url))
                            memory_store.update_operation_polling(
                                chat_id=chat_id,
                                message_id=message_id,
                                polled_status=normalized_status,
                                cursor_url=cursor_url or last_url,
                                notify_state="failure_notified",
                            )
                    return

            time.sleep(settings.agent_poll_interval_seconds)

        timeout_summary = "Agent 轮询超时，未观察到终态"
        if last_url:
            timeout_summary += f" | url={last_url}"
        memory_store.complete_operation(
            chat_id=chat_id,
            message_id=message_id,
            status="timeout",
            agent_id=agent_id,
            result_summary=timeout_summary,
            cursor_url=last_url,
            polled_status=last_status or "TIMEOUT",
        )
        memory_store.set_chat_session(
            chat_id=chat_id,
            agent_id=agent_id,
            status=last_status or "TIMEOUT",
            cursor_url=last_url,
        )
        if settings.notify_on_agent_timeout:
            notify(_build_timeout_message(last_url))
            memory_store.update_operation_polling(
                chat_id=chat_id,
                message_id=message_id,
                polled_status=last_status or "TIMEOUT",
                cursor_url=last_url,
                notify_state="timeout_notified",
            )

    except Exception as e:
        logger.exception(f"Agent 轮询异常 | agent_id={agent_id} | msg_id={message_id} | error={e}")
        memory_store.complete_operation(
            chat_id=chat_id,
            message_id=message_id,
            status="poll_error",
            agent_id=agent_id,
            result_summary=f"轮询异常: {str(e)[:120]}",
            cursor_url=last_url,
            polled_status=last_status or "POLL_ERROR",
        )
        memory_store.set_chat_session(
            chat_id=chat_id,
            agent_id=agent_id,
            status=last_status or "POLL_ERROR",
            cursor_url=last_url,
        )
    finally:
        with _poll_lock:
            _active_polls.discard(poll_key)
